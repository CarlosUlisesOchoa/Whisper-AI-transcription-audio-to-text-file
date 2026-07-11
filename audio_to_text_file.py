import argparse
import os

from transcriber import (
    AUDIO_EXTENSIONS,
    sanitize_filename,
    get_device,
    get_model,
    transcribe_audio,
    format_transcription,
)

# How to use:
# Example:
# py .\audio_to_text_file.py "D:\files\audio_folder" --language "es"


def get_audio_files_status(directory):
    to_process = []
    excluded = []

    for filename in os.listdir(directory):
        file_path = os.path.join(directory, filename)
        if not os.path.isfile(file_path):
            continue

        ext = os.path.splitext(filename)[1].lower()
        if ext not in AUDIO_EXTENSIONS:
            continue

        base_name = os.path.splitext(filename)[0]
        sanitized_txt = sanitize_filename(base_name + '.txt')
        txt_path = os.path.join(directory, sanitized_txt)

        if os.path.exists(txt_path):
            excluded.append((file_path, "Transcription text file already exists"))
        else:
            to_process.append(file_path)

    return to_process, excluded


def resolve_voices_dir(cli_value):
    """Resolve the voices directory: CLI flag > env VOICES_DIR > voices/ next to script > disabled."""
    if cli_value:
        return cli_value
    env_value = os.environ.get("VOICES_DIR")
    if env_value:
        return env_value
    script_dir = os.path.dirname(os.path.abspath(__file__))
    default_dir = os.path.join(script_dir, "voices")
    return default_dir if os.path.isdir(default_dir) else None


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
    parser.add_argument("--align", action="store_true", default=False, help="Run word-level alignment (wav2vec2)")
    parser.add_argument("--no-diarize", action="store_true", default=False, help="Disable speaker diarization")
    parser.add_argument(
        "--voices", type=str, default=None,
        help="Directory of enrolled reference voices for named speaker ID "
             "(default: env VOICES_DIR, else 'voices/' next to this script if present, else disabled)",
    )
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

    device = get_device()
    if device == "cuda":
        import torch
        gpu_name = torch.cuda.get_device_name(0)
        print(f"CUDA is available. Using GPU: {gpu_name}")
    else:
        print("CUDA is not available. Using CPU.")

    # Preload model once so first file does not pay the full cold-start cost.
    get_model()

    voices_dir = resolve_voices_dir(args.voices)
    if voices_dir:
        print(f"Named speaker ID: enabled ({voices_dir})")

    processed_files = []
    failed_files = []

    for audio_file in files_to_process:
        print(f"\nProcessing: {os.path.basename(audio_file)}")
        try:
            filename = os.path.splitext(os.path.basename(audio_file))[0]
            sanitized_txt = sanitize_filename(filename + '.txt')
            output_path = os.path.join(directory, sanitized_txt)

            print("Starting transcription...")
            diarize = False if args.no_diarize else None
            result = transcribe_audio(
                audio_file, language=args.language, align=args.align, diarize=diarize, voices_dir=voices_dir
            )

            print("Transcription completed. Saving to:", output_path)
            formatted = format_transcription(os.path.basename(output_path), result["segments"])
            with open(output_path, 'w', encoding='utf-8') as f:
                f.write(formatted)
            print(formatted, end='')
            processed_files.append(audio_file)
        except Exception as e:
            print(f"Error processing {os.path.basename(audio_file)}: {str(e)}")
            failed_files.append((audio_file, str(e)))

    print("\n" + "=" * 50)
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
