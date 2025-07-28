import fitz  # PyMuPDF
import os
import json
import re
import logging
from collections import Counter, defaultdict

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# --- Configuration ---
INPUT_DIR = "/app/input"
OUTPUT_DIR = "/app/output"

class Config:
    MAX_FILE_SIZE_MB = 50
    MAX_PAGES = 50
    HEADING_SIZE_FACTOR = 1.15
    VERTICAL_MARGIN = 0.08
    MIN_HEADING_CHARS = 2
    MAX_HEADING_WORDS = 20

def detect_language(text_sample):
    """Detect the primary language of the text"""
    if not text_sample:
        return 'en'
    
    japanese_chars = len(re.findall(r'[\u3040-\u309F\u30A0-\u30FF\u4E00-\u9FAF]', text_sample))
    if japanese_chars > len(text_sample) * 0.1:
        return 'ja'
    
    spanish_chars = len(re.findall(r'[ñáéíóúü]', text_sample.lower()))
    if spanish_chars > 0:
        return 'es'
    
    french_chars = len(re.findall(r'[àâäéèêëïîôöùûüÿç]', text_sample.lower()))
    if french_chars > 0:
        return 'fr'
    
    return 'en'

def validate_pdf_input(pdf_path):
    """PDF validation"""
    if not os.path.exists(pdf_path):
        raise FileNotFoundError(f"PDF not found: {pdf_path}")
    
    size_mb = os.path.getsize(pdf_path) / (1024 * 1024)
    if size_mb > Config.MAX_FILE_SIZE_MB:
        raise ValueError(f"PDF too large: {size_mb:.1f}MB")
    
    try:
        with fitz.open(pdf_path) as doc:
            if doc.is_encrypted:
                raise ValueError("Encrypted PDFs not supported")
            if doc.page_count > Config.MAX_PAGES:
                raise ValueError(f"Too many pages: {doc.page_count}")
    except Exception as e:
        raise ValueError(f"Invalid PDF: {e}")

def extract_text_blocks(pdf_path):
    """Extract text blocks with metadata"""
    blocks = []
    try:
        doc = fitz.open(pdf_path)
        for page_num, page in enumerate(doc):
            page_blocks = page.get_text("dict", flags=fitz.TEXTFLAGS_SEARCH)["blocks"]
            for block in page_blocks:
                if block['type'] == 0:
                    for line in block['lines']:
                        line_text = " ".join([span['text'] for span in line['spans']]).strip()
                        if not line_text or not line['spans']:
                            continue
                        
                        first_span = line['spans'][0]
                        bbox = line['bbox']
                        
                        blocks.append({
                            "text": line_text,
                            "font_size": round(first_span['size']),
                            "font_name": first_span['font'],
                            "bbox": bbox,
                            "page": page_num + 1,
                            "page_height": page.rect.height,
                            "is_bold": "bold" in first_span['font'].lower(),
                            "y_relative": bbox[1] / page.rect.height,
                            "x_relative": bbox[0] / page.rect.width
                        })
        doc.close()
    except Exception as e:
        logging.error(f"Error extracting blocks from {pdf_path}: {e}")
        raise
    return blocks

def is_table_subpoint(block, surrounding_blocks):
    """Check if block is a table sub-point"""
    text = block['text'].strip()
    
    same_row_blocks = []
    y_tolerance = 10
    
    for other_block in surrounding_blocks:
        if abs(other_block['bbox'][1] - block['bbox'][1]) < y_tolerance:
            same_row_blocks.append(other_block)
    
    if len(same_row_blocks) >= 3:
        return True
    
    if re.match(r'^\d+\.?\s*$', text) or re.match(r'^[a-zA-Z]\)\s*$', text):
        return True
    
    nearby_numbered = 0
    for other_block in surrounding_blocks:
        if abs(other_block['bbox'][1] - block['bbox'][1]) < 50:
            if re.match(r'^\d+\.', other_block['text']):
                nearby_numbered += 1
    
    return nearby_numbered >= 3

def classify_heading_by_numbering(text, language='en'):
    """Multi-language heading classification"""
    if language == 'ja':
        if re.match(r'^第?\d+[章節条項目]', text):
            return "H1"
        if re.match(r'^\d+\.\d+', text):
            return "H2"
        if re.match(r'^\d+\.\d+\.\d+', text):
            return "H3"
    else:
        if re.match(r'^\d+\.\d+\.\d+', text):
            return "H3"
        if re.match(r'^\d+\.\d+', text):
            return "H2"
        if re.match(r'^\d+\.(?!\d)', text) and len(text.split()) < 10:
            return "H1"
    
    if re.match(r'^[A-Z]\.\s', text):
        return "H2"
    
    return None

def is_plausible_heading(block, body_size, surrounding_blocks):
    """Enhanced heading detection"""
    text = block['text'].strip()
    words = text.split()
    word_count = len(words)
    
    if is_table_subpoint(block, surrounding_blocks):
        return False
    
    junk_patterns = [
        r'^S\.?No\.?', r'^Sr\.?No\.?', r'^Page\s+\d+', r'^Fig(\.|ure)?\s*\d+',
        r'^Table\s*\d+', r'^\d+\s*$', r'^[A-Za-z]\s*$', r'www\.', r'@'
    ]
    
    if any(re.match(p, text, re.IGNORECASE) for p in junk_patterns):
        return False
    
    if not (1 <= word_count <= Config.MAX_HEADING_WORDS):
        return False
    if len(text) < Config.MIN_HEADING_CHARS:
        return False
    
    if text.endswith((',', ';')) or (text.endswith('.') and word_count > 8):
        return False
    
    if block['font_size'] <= body_size and not block['is_bold']:
        return False
    
    if text.isupper() and word_count > 5:
        return False
    
    return True

def is_form_like_document(text_blocks):
    """Detect form documents"""
    first_page_blocks = [b for b in text_blocks if b['page'] == 1]
    
    form_indicators = 0
    total_lines = len(first_page_blocks)
    
    if total_lines == 0:
        return False
    
    for block in first_page_blocks:
        text = block['text'].strip()
        
        if re.match(r'^\d+\.?\s*$', text):
            form_indicators += 1
        elif 'application' in text.lower() and 'form' in text.lower():
            form_indicators += 3
        elif re.match(r'^\d+\.\s*.{1,30}$', text):
            form_indicators += 1
        elif len(text.split()) <= 3 and ':' in text:
            form_indicators += 1
    
    return (form_indicators / total_lines) > 0.4

def find_document_title(text_blocks, page_width):
    """Multi-language title detection"""
    first_page_blocks = [b for b in text_blocks if b['page'] <= 2]
    
    if not first_page_blocks:
        return "Untitled Document", None
    
    title_candidates = []
    
    for block in first_page_blocks:
        score = 0
        text = block['text'].strip()
        
        if block['y_relative'] < 0.3:
            score += 3
        
        if 0.2 < block['x_relative'] < 0.8:
            score += 2
        
        if block['is_bold']:
            score += 2
        
        avg_font_size = sum(b['font_size'] for b in first_page_blocks) / len(first_page_blocks)
        if block['font_size'] > avg_font_size * 1.2:
            score += 1
        
        word_count = len(text.split())
        if 3 <= word_count <= 15:
            score += 1
        
        title_candidates.append((score, block))
    
    title_candidates.sort(key=lambda x: x[0], reverse=True)
    best_candidate = title_candidates[0][1]
    
    return best_candidate['text'], best_candidate['bbox']

def analyze_document_styles(text_blocks):
    """Analyze font styles"""
    if not text_blocks:
        return 12, {}

    all_font_sizes = [block['font_size'] for block in text_blocks if block['text']]
    if not all_font_sizes:
        return 12, {}

    font_sizes_for_body_text = [size for size in all_font_sizes if 8 < size < 20]
    
    if not font_sizes_for_body_text:
        most_common_size = min(all_font_sizes)
    else:
        most_common_size = Counter(font_sizes_for_body_text).most_common(1)[0][0]

    unique_sizes = sorted(list(set(all_font_sizes)), reverse=True)
    heading_sizes = [s for s in unique_sizes if s >= most_common_size * Config.HEADING_SIZE_FACTOR and s > most_common_size]

    size_to_level = {size: f"H{i+1}" for i, size in enumerate(heading_sizes[:4])}
    
    return most_common_size, size_to_level

def process_pdf(pdf_path):
    """Main processing pipeline"""
    if not os.path.exists(pdf_path) or os.path.getsize(pdf_path) == 0:
        return None
        
    try:
        with fitz.open(pdf_path) as doc:
            if doc.page_count == 0:
                return {"title": "Empty Document", "outline": []}
            page_width = doc[0].rect.width
    except Exception as e:
        logging.error(f"Error opening PDF {pdf_path}: {e}")
        return None

    all_blocks = extract_text_blocks(pdf_path)
    if not all_blocks:
        return {"title": "Document with no extractable text", "outline": []}

    # Detect document language
    first_page_text = " ".join([b['text'] for b in all_blocks if b['page'] == 1])
    document_language = detect_language(first_page_text)
    logging.info(f"Detected language: {document_language}")

    body_size, size_to_level_map = analyze_document_styles(all_blocks)
    title, title_bbox = find_document_title(all_blocks, page_width)
    
    # Check if form document
    if is_form_like_document(all_blocks):
        return {"title": title, "outline": []}
    
    outline = []
    for block in all_blocks:
        if block['bbox'] == title_bbox or not block['text']:
            continue
        
        # Skip headers/footers
        if (block['y_relative'] < Config.VERTICAL_MARGIN or 
            block['y_relative'] > (1 - Config.VERTICAL_MARGIN)):
            continue
        
        # Get surrounding blocks
        surrounding_blocks = [b for b in all_blocks 
                            if b['page'] == block['page'] and 
                            abs(b['bbox'][1] - block['bbox'][1]) < 100]
        
        if not is_plausible_heading(block, body_size, surrounding_blocks):
            continue

        level = None
        level_from_num = classify_heading_by_numbering(block['text'], document_language)
        if level_from_num:
            level = level_from_num
        elif block['font_size'] in size_to_level_map:
            level = size_to_level_map[block['font_size']]
        elif block['is_bold'] and block['font_size'] > body_size:
            level = "H3"

        if level:
            outline.append({
                "level": level,
                "text": block['text'],
                "page": block['page']
            })
    
    # Remove duplicates
    unique_outline = []
    seen = set()
    for item in outline:
        identifier = (item['text'], item['page'])
        if identifier not in seen:
            unique_outline.append(item)
            seen.add(identifier)
    
    return {"title": title, "outline": unique_outline}

def main():
    """Main entry point for Round 1A"""
    try:
        if not os.path.exists(OUTPUT_DIR):
            os.makedirs(OUTPUT_DIR)

        pdf_files = [f for f in os.listdir(INPUT_DIR) if f.lower().endswith('.pdf')]
        
        if not pdf_files:
            logging.warning("No PDF files found in input directory")
            return
        
        success_count = 0
        total_files = len(pdf_files)
        
        for pdf_file in pdf_files:
            input_path = os.path.join(INPUT_DIR, pdf_file)
            output_filename = os.path.splitext(pdf_file)[0] + ".json"
            output_path = os.path.join(OUTPUT_DIR, output_filename)
            
            logging.info(f"Processing {pdf_file} ({pdf_files.index(pdf_file) + 1}/{total_files})")
            
            try:
                validate_pdf_input(input_path)
                result = process_pdf(input_path)
                
                if result:
                    with open(output_path, 'w', encoding='utf-8') as f:
                        json.dump(result, f, indent=2, ensure_ascii=False)
                    logging.info(f"✓ Successfully generated {output_filename}")
                    success_count += 1
                else:
                    logging.error(f"✗ Failed to process {pdf_file}")
            except Exception as e:
                logging.error(f"✗ Error processing {pdf_file}: {e}")
        
        logging.info(f"Processing complete: {success_count}/{total_files} files successful")
        
    except Exception as e:
        logging.error(f"Fatal error in main: {e}")
        raise

if __name__ == "__main__":
    main()
