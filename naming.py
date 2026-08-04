import os
import re
import unicodedata

AUDIO_EXTENSIONS = {'.mp3', '.wav', '.m4a', '.ogg', '.flac'}

ACCENTED_VOWEL_TRANSLATION = str.maketrans("áéíóú", "aeiou")


def sanitize_filename(filename):
    """Sanitize a filename: lowercase, replace unknown characters with dashes, collapse dashes and transliterate accented vowels."""
    base_name = os.path.splitext(filename)[0]
    extension = os.path.splitext(filename)[1]

    # Normalize first so composed and decomposed accents are treated the same.
    sanitized = unicodedata.normalize("NFC", base_name.lower())
    sanitized = sanitized.translate(ACCENTED_VOWEL_TRANSLATION)
    sanitized = re.sub(r'[^a-z0-9-_]', '-', sanitized)
    sanitized = re.sub(r'-+', '-', sanitized)
    sanitized = sanitized.strip('-')

    return sanitized + extension
