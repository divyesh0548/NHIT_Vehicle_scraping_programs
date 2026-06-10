import cv2
import numpy as np
from transformers import TrOCRProcessor, VisionEncoderDecoderModel
import easyocr
from PIL import Image
import torch

# Initialize models once (load at module level)
processor = TrOCRProcessor.from_pretrained("microsoft/trocr-large-printed")
model = VisionEncoderDecoderModel.from_pretrained("microsoft/trocr-large-printed")
# Explicitly move model to CPU to avoid device mismatch issues
model = model.to("cpu")
model.eval()  # Set to evaluation mode
easyocr_reader = easyocr.Reader(['en'], gpu=False)  # Set gpu=True if you have CUDA


def tr_ocr_image_from_array(image_array):
    """
    Best for single line captcha text
    Input: numpy array
    Output: List of detected text strings
    """
    img = Image.fromarray(image_array).convert("RGB")
    pixel_values = processor(images=img, return_tensors="pt").pixel_values
    # Ensure pixel_values are on CPU
    pixel_values = pixel_values.to("cpu")
    # Generate with model on CPU
    with torch.no_grad():
        generated_ids = model.generate(pixel_values)
    return processor.batch_decode(generated_ids, skip_special_tokens=True)


def tr_ocr_split_horizontally(image_array):
    """
    Best for stacked/multi-line captcha text
    Input: numpy array
    Output: List of detected text strings (one per line)
    """
    # Split image horizontally into two parts
    height, width = image_array.shape[:2]
    mid_height = height // 2
    
    # Top half
    top_half = image_array[:mid_height, :]
    top_pil = Image.fromarray(top_half).convert("RGB")
    
    # Bottom half
    bottom_half = image_array[mid_height:, :]
    bottom_pil = Image.fromarray(bottom_half).convert("RGB")
    
    all_texts = []
    
    # Process each half
    for half_img in [top_pil, bottom_pil]:
        pixel_values = processor(images=half_img, return_tensors="pt").pixel_values
        # Ensure pixel_values are on CPU
        pixel_values = pixel_values.to("cpu")
        # Generate with model on CPU
        with torch.no_grad():
            generated_ids = model.generate(pixel_values)
        text = processor.batch_decode(generated_ids, skip_special_tokens=True)[0]
        if text.strip():  # Only add non-empty text
            all_texts.append(text.strip())
    
    return all_texts


def easy_ocr_image_from_array(image_array):
    """
    Fallback method using EasyOCR + TrOCR combination
    Best for complex/unclear captchas
    Input: numpy array
    Output: List of detected text strings
    """
    # Detect text regions
    results = easyocr_reader.readtext(image_array)
    
    all_texts = []
    
    for (bbox, _, confidence) in results:
        if confidence > 0.5:  # Filter by confidence
            # Extract bounding box coordinates
            x_min = int(min([p[0] for p in bbox]))
            y_min = int(min([p[1] for p in bbox]))
            x_max = int(max([p[0] for p in bbox]))
            y_max = int(max([p[1] for p in bbox]))
            
            # Crop the text region
            cropped = image_array[y_min:y_max, x_min:x_max]
            cropped_pil = Image.fromarray(cropped).convert("RGB")
            
            # Apply TrOCR
            pixel_values = processor(images=cropped_pil, return_tensors="pt").pixel_values
            # Ensure pixel_values are on CPU
            pixel_values = pixel_values.to("cpu")
            # Generate with model on CPU
            with torch.no_grad():
                generated_ids = model.generate(pixel_values)
            text = processor.batch_decode(generated_ids, skip_special_tokens=True)[0]
            all_texts.append(text.strip())
    
    return all_texts


def special_character_remover(raw_output):
    """
    Remove any special characters from detected text
    Input: str
    Output: str (cleaned)
    """
    if not raw_output:
        return ""
    
    import re
    cleaned = re.sub(r'[^A-Za-z0-9]', '', raw_output)
    return cleaned


def extract_text(cropped_image):

    
    # Method 1: Try TrOCR for single line
    extracted_text = tr_ocr_image_from_array(cropped_image)
    temp_text = " ".join(extracted_text)
    
    if len(temp_text) >= 3:  # If result is good enough
        return extracted_text
    
    # Method 2: Try splitting horizontally (for stacked text)
    extracted_text = tr_ocr_split_horizontally(cropped_image)
    temp_text = " ".join(extracted_text)
    
    if len(temp_text) >= 3:  # If result is good enough
        return extracted_text
    
    # Method 3: Fallback to EasyOCR
    extracted_text = easy_ocr_image_from_array(cropped_image)
    return extracted_text
