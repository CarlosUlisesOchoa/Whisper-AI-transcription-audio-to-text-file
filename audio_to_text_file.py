import torch
import whisper
import argparse
import os
import re  # Add this import at the top

# How to use:
# Example:
# py .\audio_to_text_file.py "D:\Shane\Download\wolverin\1-pending\myFilename" --language "es"

def sanitize_filename(filename):
    # Remove file extension
    base_name = os.path.splitext(filename)[0]
    extension = os.path.splitext(filename)[1]
    
    # Convert to lowercase and replace spaces with dashes
    sanitized = base_name.lower()
    # Replace any character that's not alphanumeric, dash, or underscore with dash
    sanitized = re.sub(r'[^a-z0-9-_]', '-', sanitized)
    # Replace multiple dashes with single dash
    sanitized = re.sub(r'-+', '-', sanitized)
    # Remove leading/trailing dashes
    sanitized = sanitized.strip('-')
    
    return sanitized + extension

def get_audio_files_status(directory):
    audio_extensions = {'.mp3', '.wav', '.m4a', '.ogg', '.flac'}
    to_process = []
    excluded = []
    
    for filename in os.listdir(directory):
        file_path = os.path.join(directory, filename)
        if not os.path.isfile(file_path):
            continue
            
        ext = os.path.splitext(filename)[1].lower()
        if ext not in audio_extensions:
            continue
            
        base_name = os.path.splitext(filename)[0]
        sanitized_txt = sanitize_filename(base_name + '.txt')
        txt_path = os.path.join(directory, sanitized_txt)
        
        if os.path.exists(txt_path):
            excluded.append((file_path, "Transcription text file already exists"))
        else:
            to_process.append(file_path)
    
    return to_process, excluded

def print_file_status(to_process, excluded):
    print("\nFiles to be processed:")
    if to_process:
        for file in to_process:
            print(f"✓ {os.path.basename(file)}")
    else:
        print("None")
    
    print("\nExcluded files:")
    if excluded:
        for file, reason in excluded:
            print(f"✗ {os.path.basename(file)} - {reason}")
    else:
        print("None")

def main():
    parser = argparse.ArgumentParser(description="Transcribe audio files in a directory using Whisper model.")
    parser.add_argument("directory", type=str, help="Directory containing audio files")
    parser.add_argument("--language", type=str, default=None, help="Language of the audio")
    parser.add_argument("--accept", action="store_true", help="Auto-accept file list without confirmation")
    args = parser.parse_args()

    directory = os.path.abspath(args.directory)
    files_to_process, excluded_files = get_audio_files_status(directory)
    
    if not files_to_process:
        print("No new audio files to process.")
        return

    print_file_status(files_to_process, excluded_files)
    
    if not args.accept:
        confirmation = input("\nProceed with processing? (y/N): ").lower()
        if confirmation != 'y':
            print("Operation cancelled.")
            return

    # Initialize device and model (only once)
    if torch.cuda.is_available():
        device = "cuda"
        gpu_name = torch.cuda.get_device_name(0)
        print(f"CUDA is available. Using GPU: {gpu_name}")
    else:
        device = "cpu"
        print("CUDA is not available. Using CPU.")

    model = whisper.load_model("turbo", device=device)

    processed_files = []
    failed_files = []

    # Process each file
    for audio_file in files_to_process:
        print(f"\nProcessing: {os.path.basename(audio_file)}")
        try:
            # Generate output path
            filename = os.path.splitext(os.path.basename(audio_file))[0]
            sanitized_filename = sanitize_filename(filename + '.txt')
            output_path = os.path.join(directory, sanitized_filename)
            
            # Transcribe
            print("Starting transcription...")
            result = model.transcribe(audio_file, language=args.language)

            # Save transcription
            print("Transcription completed. Saving to:", output_path)
            with open(output_path, 'w', encoding='utf-8') as f:
                f.write('='*50 + '\n')
                f.write(f"filename:{os.path.basename(output_path)}\n")
                f.write('='*50 + '\n')
                for segment in result["segments"]:
                    start = segment["start"]
                    end = segment["end"]
                    text = segment["text"]
                    line = f"[{start:.2f}s - {end:.2f}s] {text}\n"
                    f.write(line)
                    print(line, end='')
            processed_files.append(audio_file)
        except Exception as e:
            print(f"Error processing {os.path.basename(audio_file)}: {str(e)}")
            failed_files.append((audio_file, str(e)))

    # Print final summary
    print("\n" + "="*50)
    print("Processing Complete!")
    print("\nSuccessfully processed files:")
    if processed_files:
        for file in processed_files:
            print(f"✓ {os.path.basename(file)}")
    else:
        print("None")

    print("\nFailed files:")
    if failed_files:
        for file, error in failed_files:
            print(f"✗ {os.path.basename(file)} - {error}")
    else:
        print("None")

    print("\nPreviously excluded files:")
    if excluded_files:
        for file, reason in excluded_files:
            print(f"- {os.path.basename(file)} - {reason}")
    else:
        print("None")

if __name__ == "__main__":
    main()
