"""
Translation backends: google, deepl, claude.
Each backend exposes a translate(text, target_lang) function.
Language detection uses deep-translator's detection where available,
otherwise relies on the translation API to handle it transparently.
"""

from typing import Optional


def _make_google(target_lang: str):
    from deep_translator import GoogleTranslator, single_detection

    def translate(text: str) -> Optional[str]:
        try:
            detected = single_detection(text, api_key=None)
        except Exception:
            detected = None

        # Skip translation if already in target language
        if detected and detected == target_lang:
            return None

        try:
            result = GoogleTranslator(source="auto", target=target_lang).translate(text)
            # Return None if unchanged (e.g. already English, or very short string)
            if result and result.strip() != text.strip():
                return result
        except Exception as e:
            print(f"[translator] Google error: {e}")
        return None

    return translate


def _make_deepl(target_lang: str, api_key: str):
    import requests

    session = requests.Session()
    # DeepL free API uses api-free.deepl.com; paid uses api.deepl.com
    base = "https://api-free.deepl.com/v2/translate"

    def translate(text: str) -> Optional[str]:
        try:
            resp = session.post(
                base,
                data={
                    "auth_key": api_key,
                    "text": text,
                    "target_lang": target_lang.upper(),
                },
                timeout=5,
            )
            if resp.status_code == 200:
                result = resp.json()["translations"][0]
                src = result.get("detected_source_language", "").lower()
                translated = result["text"]
                if src and src == target_lang.lower():
                    return None  # already target language
                if translated.strip() != text.strip():
                    return translated
        except Exception as e:
            print(f"[translator] DeepL error: {e}")
        return None

    return translate


def build(backend: str, target_lang: str, deepl_api_key: str = "", **_kwargs):
    """Return a translate(text) -> Optional[str] callable for the chosen backend."""
    if backend == "deepl":
        if not deepl_api_key:
            raise ValueError("deepl_api_key is required for the DeepL backend")
        return _make_deepl(target_lang, deepl_api_key)
    else:
        if backend not in ("google", "deepl"):
            print(f"[translator] Unknown backend '{backend}', falling back to google")
        return _make_google(target_lang)
