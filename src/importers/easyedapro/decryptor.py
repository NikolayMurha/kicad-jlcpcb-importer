import gzip
import io
import platform

_AES_IMPORT_ERROR = None

def _build_aes_error_message():
    if platform.system() == "Windows":
        return (
            "Please install PyCryptodome in KiCad Command Prompt using: "
            "pip install pycryptodome --target ./lib"
        )
    return "Please install PyCryptodome using: pip install pycryptodome --target ./lib"

def _load_aes():
    global _AES_IMPORT_ERROR
    try:
        from Crypto.Cipher import AES
        return AES
    except Exception:
        try:
            from Cryptodome.Cipher import AES
            return AES
        except Exception as exc:
            _AES_IMPORT_ERROR = exc
            return None

AES = _load_aes()

def _ensure_aes():
    global AES
    if AES is None:
        AES = _load_aes()
    return AES

def decryptDataStrIdData(encoded_data, key_hex, iv_hex):
    aes = _ensure_aes()
    if aes is None:
        msg = _build_aes_error_message()
        if _AES_IMPORT_ERROR is not None:
            raise Exception(msg) from _AES_IMPORT_ERROR
        raise Exception(msg)
    # Convert hex strings to bytes
    key = bytes.fromhex(key_hex)
    iv = bytes.fromhex(iv_hex)
    
    # Create AES-GCM cipher
    cipher = aes.new(key, aes.MODE_GCM, nonce=iv)
    
    # In AES-GCM, typically the last 16 bytes are the authentication tag
    tag = encoded_data[-16:]
    actual_ciphertext = encoded_data[:-16]
    
    # Decrypt data
    plaintext = cipher.decrypt_and_verify(actual_ciphertext, tag)
    
    # Decompress with gzip
    with io.BytesIO(plaintext) as compressed_data:
        with gzip.GzipFile(fileobj=compressed_data, mode='rb') as f:
            decompressed_data = f.read()
    
    return decompressed_data.decode('utf-8')
