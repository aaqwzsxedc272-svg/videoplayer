"""Public mega.nz links: read the name, then download and decrypt.

A mega.nz URL carries the decryption key in the fragment. The file on the
CDN is AES-encrypted, so mpv cannot open the link itself. This module talks
to MEGA's public API and decrypts the bytes. It does not log in.

AES comes from PyCryptodome or the cryptography package when one of those
is installed. Otherwise it uses OpenSSL, then Windows bcrypt, then a small
built-in AES-128. A movie is slow only on that last path.
"""

from __future__ import annotations

import base64
import http.server
import json
import os
import random
import re
import secrets
import socket
import struct
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

_API_HOSTS = (
    'https://g.api.mega.co.nz/cs',
    'https://eu.api.mega.co.nz/cs',
    'https://api.mega.co.nz/cs',
)
_UA = (
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
    'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0 Safari/537.36'
)
_MEDIA_EXT = {
    '.mp4', '.mkv', '.webm', '.avi', '.mov', '.m4v', '.wmv', '.flv',
    '.ts', '.mpg', '.mpeg', '.m2ts', '.mp3', '.m4a', '.flac', '.aac',
    '.ogg', '.wav', '.opus', '.jpg', '.jpeg', '.png', '.webp', '.gif',
}
_SUB_EXT = {'.srt', '.vtt', '.ass', '.ssa'}
_ERRORS = {
    -9: 'MEGA link does not exist or was removed',
    -11: 'MEGA refused this link',
    -16: 'MEGA blocked this file',
    -17: 'MEGA download quota is used up — try again later',
    -18: 'MEGA is temporarily unavailable — try again in a minute',
}
_MASK128 = (1 << 128) - 1
_FIPS_KEY = bytes.fromhex('000102030405060708090a0b0c0d0e0f')
_FIPS_PLAIN = bytes.fromhex('00112233445566778899aabbccddeeff')
_FIPS_CIPHER = bytes.fromhex('69c4e0d86a7b0430d8cdb78070b4c55a')


class MegaError(Exception):
    def __init__(self, message, code=None):
        super().__init__(message)
        self.code = code


class MegaLink(object):
    def __init__(self, kind, file_id='', file_key='', folder_id='',
                 folder_key='', node_id=''):
        self.kind = kind
        self.file_id = file_id
        self.file_key = file_key
        self.folder_id = folder_id
        self.folder_key = folder_key
        self.node_id = node_id


def format_size(n):
    n = int(n or 0)
    if n >= 1 << 30:
        return f'{n / (1 << 30):.2f} GB'
    if n >= 1 << 20:
        return f'{n / (1 << 20):.1f} MB'
    if n >= 1 << 10:
        return f'{n / (1 << 10):.0f} KB'
    return f'{n} B'


def parse_mega_url(url):
    """Return a MegaLink, or None if this is not a mega.nz link."""
    text = str(url or '').strip()
    if '](http' in text:
        text = text.split('](', 1)[0]
    found = re.search(
        r'https?://(?:www\.)?mega\.(?:nz|co\.nz)/\S+', text, re.IGNORECASE)
    if found:
        text = found.group(0).rstrip('.,);]>`\'"')
    try:
        parsed = urllib.parse.urlparse(text)
    except Exception:
        return None
    host = (parsed.netloc or '').lower()
    if 'mega.nz' not in host and 'mega.co.nz' not in host:
        return None
    path = parsed.path or ''
    frag = urllib.parse.unquote(parsed.fragment or '')
    if frag.startswith('!'):
        parts = [part for part in frag.split('!')]
        # '!' / id / key  or  '!' / folder / key / file
        if len(parts) >= 3 and parts[1] and parts[2]:
            if len(parts) >= 4 and parts[3]:
                return MegaLink(
                    'folder_file', folder_id=parts[1], folder_key=parts[2],
                    node_id=parts[3])
            return MegaLink('file', file_id=parts[1], file_key=parts[2])
    if frag.startswith('F!'):
        parts = frag.split('!')
        if len(parts) >= 3 and parts[1] and parts[2]:
            if len(parts) >= 4 and parts[3]:
                return MegaLink(
                    'folder_file', folder_id=parts[1], folder_key=parts[2],
                    node_id=parts[3])
            return MegaLink('folder', folder_id=parts[1], folder_key=parts[2])
    file_m = re.match(r'^/(?:file|embed)/([^/?#]+)', path, re.IGNORECASE)
    if file_m and frag:
        return MegaLink(
            'file', file_id=file_m.group(1), file_key=frag.split('/')[0])
    folder_m = re.match(r'^/folder/([^/?#]+)', path, re.IGNORECASE)
    if folder_m and frag:
        key = frag.split('/')[0]
        node = re.search(r'(?:^|/)file/([^/]+)', frag, re.IGNORECASE)
        if node:
            return MegaLink(
                'folder_file', folder_id=folder_m.group(1), folder_key=key,
                node_id=node.group(1))
        return MegaLink('folder', folder_id=folder_m.group(1), folder_key=key)
    return None


def folder_file_url(folder_id, folder_key, file_id):
    return f'https://mega.nz/folder/{folder_id}#{folder_key}/file/{file_id}'


def _base64_url_decode(data):
    text = str(data or '').replace('-', '+').replace('_', '/').replace(',', '')
    text += '=' * ((4 - len(text) % 4) % 4)
    return base64.b64decode(text)


def _base64_url_encode(data):
    text = base64.b64encode(data).decode('ascii')
    return text.replace('+', '-').replace('/', '_').rstrip('=')


def _a32_to_bytes(values):
    return struct.pack('>%dI' % len(values), *values)


def _bytes_to_a32(data):
    if len(data) % 4:
        data += b'\0' * (4 - len(data) % 4)
    count = len(data) // 4
    return list(struct.unpack('>%dI' % count, data))


def _base64_to_a32(text):
    return _bytes_to_a32(_base64_url_decode(text))


def _xor_bytes(left, right, n=None):
    n = len(left) if n is None else n
    try:
        import numpy as np
        a = np.frombuffer(left, dtype=np.uint8, count=n)
        b = np.frombuffer(right, dtype=np.uint8, count=n)
        return np.bitwise_xor(a, b).tobytes()
    except Exception:
        return bytes(a ^ b for a, b in zip(left[:n], right[:n]))


def _counter_blocks(start, count):
    """count big-endian 16-byte counters beginning at start."""
    if count <= 0:
        return b''
    try:
        import numpy as np
        hi = np.uint64(start >> 64)
        lo = np.uint64(start & 0xFFFFFFFFFFFFFFFF)
        stacked = np.empty((count, 2), dtype=np.uint64)
        stacked[:, 0] = hi
        stacked[:, 1] = lo + np.arange(count, dtype=np.uint64)
        if sys.byteorder == 'little':
            stacked = stacked.byteswap()
        return stacked.tobytes()
    except Exception:
        raw = bytearray(count * 16)
        n = start
        for i in range(count):
            raw[i * 16:(i + 1) * 16] = n.to_bytes(16, 'big')
            n = (n + 1) & _MASK128
        return bytes(raw)


# ── AES-128. Fast library when present, otherwise the tables below. ─────────

_SBOX = (
    0x63, 0x7c, 0x77, 0x7b, 0xf2, 0x6b, 0x6f, 0xc5, 0x30, 0x01, 0x67, 0x2b, 0xfe, 0xd7, 0xab, 0x76,
    0xca, 0x82, 0xc9, 0x7d, 0xfa, 0x59, 0x47, 0xf0, 0xad, 0xd4, 0xa2, 0xaf, 0x9c, 0xa4, 0x72, 0xc0,
    0xb7, 0xfd, 0x93, 0x26, 0x36, 0x3f, 0xf7, 0xcc, 0x34, 0xa5, 0xe5, 0xf1, 0x71, 0xd8, 0x31, 0x15,
    0x04, 0xc7, 0x23, 0xc3, 0x18, 0x96, 0x05, 0x9a, 0x07, 0x12, 0x80, 0xe2, 0xeb, 0x27, 0xb2, 0x75,
    0x09, 0x83, 0x2c, 0x1a, 0x1b, 0x6e, 0x5a, 0xa0, 0x52, 0x3b, 0xd6, 0xb3, 0x29, 0xe3, 0x2f, 0x84,
    0x53, 0xd1, 0x00, 0xed, 0x20, 0xfc, 0xb1, 0x5b, 0x6a, 0xcb, 0xbe, 0x39, 0x4a, 0x4c, 0x58, 0xcf,
    0xd0, 0xef, 0xaa, 0xfb, 0x43, 0x4d, 0x33, 0x85, 0x45, 0xf9, 0x02, 0x7f, 0x50, 0x3c, 0x9f, 0xa8,
    0x51, 0xa3, 0x40, 0x8f, 0x92, 0x9d, 0x38, 0xf5, 0xbc, 0xb6, 0xda, 0x21, 0x10, 0xff, 0xf3, 0xd2,
    0xcd, 0x0c, 0x13, 0xec, 0x5f, 0x97, 0x44, 0x17, 0xc4, 0xa7, 0x7e, 0x3d, 0x64, 0x5d, 0x19, 0x73,
    0x60, 0x81, 0x4f, 0xdc, 0x22, 0x2a, 0x90, 0x88, 0x46, 0xee, 0xb8, 0x14, 0xde, 0x5e, 0x0b, 0xdb,
    0xe0, 0x32, 0x3a, 0x0a, 0x49, 0x06, 0x24, 0x5c, 0xc2, 0xd3, 0xac, 0x62, 0x91, 0x95, 0xe4, 0x79,
    0xe7, 0xc8, 0x37, 0x6d, 0x8d, 0xd5, 0x4e, 0xa9, 0x6c, 0x56, 0xf4, 0xea, 0x65, 0x7a, 0xae, 0x08,
    0xba, 0x78, 0x25, 0x2e, 0x1c, 0xa6, 0xb4, 0xc6, 0xe8, 0xdd, 0x74, 0x1f, 0x4b, 0xbd, 0x8b, 0x8a,
    0x70, 0x3e, 0xb5, 0x66, 0x48, 0x03, 0xf6, 0x0e, 0x61, 0x35, 0x57, 0xb9, 0x86, 0xc1, 0x1d, 0x9e,
    0xe1, 0xf8, 0x98, 0x11, 0x69, 0xd9, 0x8e, 0x94, 0x9b, 0x1e, 0x87, 0xe9, 0xce, 0x55, 0x28, 0xdf,
    0x8c, 0xa1, 0x89, 0x0d, 0xbf, 0xe6, 0x42, 0x68, 0x41, 0x99, 0x2d, 0x0f, 0xb0, 0x54, 0xbb, 0x16,
)
_INV_SBOX = [0] * 256
for _i, _v in enumerate(_SBOX):
    _INV_SBOX[_v] = _i
_RCON = (0x00, 0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80, 0x1b, 0x36)


def _xtime(a):
    return ((a << 1) ^ 0x1b) & 0xff if a & 0x80 else (a << 1) & 0xff


def _expand_key(key):
    words = list(struct.unpack('>4I', key))
    for i in range(4, 44):
        temp = words[i - 1]
        if i % 4 == 0:
            temp = ((temp << 8) | (temp >> 24)) & 0xffffffff
            b = temp.to_bytes(4, 'big')
            temp = (
                (_SBOX[b[0]] << 24) | (_SBOX[b[1]] << 16)
                | (_SBOX[b[2]] << 8) | _SBOX[b[3]]
            )
            temp ^= (_RCON[i // 4] << 24)
        words.append(words[i - 4] ^ temp)
    return words


def _add_round(state, words, rnd):
    rk = struct.pack('>4I', *words[rnd * 4:rnd * 4 + 4])
    return bytes(s ^ k for s, k in zip(state, rk))


def _sub_bytes(state, box):
    return bytes(box[b] for b in state)


def _shift_rows(state):
    s = state
    return bytes((
        s[0], s[5], s[10], s[15],
        s[4], s[9], s[14], s[3],
        s[8], s[13], s[2], s[7],
        s[12], s[1], s[6], s[11],
    ))


def _inv_shift_rows(state):
    s = state
    return bytes((
        s[0], s[13], s[10], s[7],
        s[4], s[1], s[14], s[11],
        s[8], s[5], s[2], s[15],
        s[12], s[9], s[6], s[3],
    ))


def _mix_column(col, decrypt=False):
    if not decrypt:
        a, b, c, d = col
        return (
            _xtime(a) ^ _xtime(b) ^ b ^ c ^ d,
            a ^ _xtime(b) ^ _xtime(c) ^ c ^ d,
            a ^ b ^ _xtime(c) ^ _xtime(d) ^ d,
            _xtime(a) ^ a ^ b ^ c ^ _xtime(d),
        )
    a, b, c, d = col
    u = _xtime(_xtime(a ^ c))
    v = _xtime(_xtime(b ^ d))
    return _mix_column((a ^ u, b ^ v, c ^ u, d ^ v), decrypt=False)


def _mix_columns(state, decrypt=False):
    out = [0] * 16
    for c in range(4):
        col = [state[r + 4 * c] for r in range(4)]
        mixed = _mix_column(col, decrypt)
        for r in range(4):
            out[r + 4 * c] = mixed[r]
    return bytes(out)


def _aes_block_encrypt(block, words):
    state = _add_round(block, words, 0)
    for rnd in range(1, 10):
        state = _sub_bytes(state, _SBOX)
        state = _shift_rows(state)
        state = _mix_columns(state, False)
        state = _add_round(state, words, rnd)
    state = _sub_bytes(state, _SBOX)
    state = _shift_rows(state)
    return _add_round(state, words, 10)


def _aes_block_decrypt(block, words):
    state = _add_round(block, words, 10)
    for rnd in range(9, 0, -1):
        state = _inv_shift_rows(state)
        state = _sub_bytes(state, _INV_SBOX)
        state = _add_round(state, words, rnd)
        state = _mix_columns(state, True)
    state = _inv_shift_rows(state)
    state = _sub_bytes(state, _INV_SBOX)
    return _add_round(state, words, 0)


class _PythonCipher(object):
    def __init__(self, key):
        self.key = key
        self.words = _expand_key(key)

    def ecb_encrypt(self, data):
        words = self.words
        out = bytearray()
        for i in range(0, len(data), 16):
            out.extend(_aes_block_encrypt(data[i:i + 16], words))
        return bytes(out)

    def ecb_decrypt(self, data):
        words = self.words
        out = bytearray()
        for i in range(0, len(data), 16):
            out.extend(_aes_block_decrypt(data[i:i + 16], words))
        return bytes(out)

    def cbc_encrypt(self, iv, data):
        out = bytearray()
        prev = iv
        for i in range(0, len(data), 16):
            block = bytes(p ^ c for p, c in zip(data[i:i + 16], prev))
            prev = _aes_block_encrypt(block, self.words)
            out.extend(prev)
        return bytes(out)

    def cbc_decrypt(self, iv, data):
        out = bytearray()
        prev = iv
        for i in range(0, len(data), 16):
            block = data[i:i + 16]
            plain = bytes(p ^ c for p, c in zip(_aes_block_decrypt(block, self.words), prev))
            out.extend(plain)
            prev = block
        return bytes(out)

    def ctr_xor(self, counter, data):
        if not data:
            return b'', counter
        blocks = (len(data) + 15) // 16
        stream = self.ecb_encrypt(_counter_blocks(counter, blocks))
        return _xor_bytes(data, stream, len(data)), (counter + blocks) & _MASK128

    def close(self):
        return None


class _LibraryCipher(object):
    """PyCryptodome or cryptography. Both speak ECB, CBC and CTR."""

    def __init__(self, key, kind, lib):
        self.key = key
        self.kind = kind
        self.lib = lib

    def _cryptodome(self, mode, iv=None, counter=None):
        AES = self.lib
        if counter is not None:
            return AES.new(self.key, mode, counter=counter)
        if iv is None:
            return AES.new(self.key, mode)
        return AES.new(self.key, mode, iv)

    def ecb_encrypt(self, data):
        if self.kind == 'cryptodome':
            return self._cryptodome(self.lib.MODE_ECB).encrypt(data)
        encryptor = self.lib.Cipher(
            self.lib.algorithms.AES(self.key), self.lib.modes.ECB()).encryptor()
        return encryptor.update(data) + encryptor.finalize()

    def ecb_decrypt(self, data):
        if self.kind == 'cryptodome':
            return self._cryptodome(self.lib.MODE_ECB).decrypt(data)
        decryptor = self.lib.Cipher(
            self.lib.algorithms.AES(self.key), self.lib.modes.ECB()).decryptor()
        return decryptor.update(data) + decryptor.finalize()

    def cbc_encrypt(self, iv, data):
        if not data:
            return b''
        if self.kind == 'cryptodome':
            return self._cryptodome(self.lib.MODE_CBC, iv=iv).encrypt(data)
        encryptor = self.lib.Cipher(
            self.lib.algorithms.AES(self.key), self.lib.modes.CBC(iv)).encryptor()
        return encryptor.update(data) + encryptor.finalize()

    def cbc_decrypt(self, iv, data):
        if not data:
            return b''
        if self.kind == 'cryptodome':
            return self._cryptodome(self.lib.MODE_CBC, iv=iv).decrypt(data)
        decryptor = self.lib.Cipher(
            self.lib.algorithms.AES(self.key), self.lib.modes.CBC(iv)).decryptor()
        return decryptor.update(data) + decryptor.finalize()

    def ctr_xor(self, counter, data):
        if not data:
            return b'', counter
        iv = counter.to_bytes(16, 'big')
        if self.kind == 'cryptodome':
            ctr = self.lib.Counter.new(128, initial_value=counter)
            out = self._cryptodome(self.lib.MODE_CTR, counter=ctr).decrypt(data)
        else:
            encryptor = self.lib.Cipher(
                self.lib.algorithms.AES(self.key), self.lib.modes.CTR(iv)).encryptor()
            out = encryptor.update(data) + encryptor.finalize()
        return out, (counter + (len(data) + 15) // 16) & _MASK128

    def close(self):
        return None


class _OpenSslCipher(object):
    def __init__(self, key, lib):
        self.key = key
        self.lib = lib

    def _evp(self, cipher_fn, iv, data, encrypt=True):
        import ctypes
        lib = self.lib
        ctx = lib.EVP_CIPHER_CTX_new()
        if not ctx:
            raise MegaError('OpenSSL could not start AES')
        try:
            init = lib.EVP_EncryptInit_ex if encrypt else lib.EVP_DecryptInit_ex
            update = lib.EVP_EncryptUpdate if encrypt else lib.EVP_DecryptUpdate
            final = lib.EVP_EncryptFinal_ex if encrypt else lib.EVP_DecryptFinal_ex
            if init(ctx, cipher_fn(), None, self.key, iv) != 1:
                raise MegaError('OpenSSL AES init failed')
            lib.EVP_CIPHER_CTX_set_padding(ctx, 0)
            out = ctypes.create_string_buffer(len(data) + 32)
            outlen = ctypes.c_int()
            if update(ctx, out, ctypes.byref(outlen), data, len(data)) != 1:
                raise MegaError('OpenSSL AES failed')
            extra = ctypes.c_int()
            final(ctx, ctypes.cast(
                ctypes.addressof(out) + outlen.value, ctypes.c_void_p),
                ctypes.byref(extra))
            total = outlen.value + max(0, extra.value)
            return out.raw[:total]
        finally:
            lib.EVP_CIPHER_CTX_free(ctx)

    def ecb_encrypt(self, data):
        return self._evp(self.lib.EVP_aes_128_ecb, None, data, True)

    def ecb_decrypt(self, data):
        return self._evp(self.lib.EVP_aes_128_ecb, None, data, False)

    def cbc_encrypt(self, iv, data):
        if not data:
            return b''
        return self._evp(self.lib.EVP_aes_128_cbc, iv, data, True)

    def cbc_decrypt(self, iv, data):
        if not data:
            return b''
        return self._evp(self.lib.EVP_aes_128_cbc, iv, data, False)

    def ctr_xor(self, counter, data):
        if not data:
            return b'', counter
        out = self._evp(
            self.lib.EVP_aes_128_ctr, counter.to_bytes(16, 'big'), data, True)
        return out, (counter + (len(data) + 15) // 16) & _MASK128

    def close(self):
        return None


class _BcryptCipher(object):
    """Windows CNG. CTR is ECB of the counter blocks; CBC is native."""

    def __init__(self, key, bcrypt, alg_ecb, alg_cbc):
        self.key = key
        self.bcrypt = bcrypt
        self._alive = []
        self.h_ecb, self._ecb_obj = self._make_key(alg_ecb)
        self.h_cbc, self._cbc_obj = self._make_key(alg_cbc)

    def _make_key(self, alg):
        bcrypt = self.bcrypt
        obj_len = ctypes.c_ulong()
        copied = ctypes.c_ulong()
        status = bcrypt.BCryptGetProperty(
            alg, 'ObjectLength', ctypes.byref(obj_len), 4,
            ctypes.byref(copied), 0)
        if status != 0:
            raise MegaError(f'Windows AES key setup failed ({status})')
        obj = ctypes.create_string_buffer(obj_len.value)
        secret = ctypes.create_string_buffer(self.key, 16)
        handle = ctypes.c_void_p()
        status = bcrypt.BCryptGenerateSymmetricKey(
            alg, ctypes.byref(handle), obj, obj_len.value, secret, 16, 0)
        if status != 0 or not handle.value:
            raise MegaError(f'Windows AES key failed ({status})')
        self._alive.append(secret)
        self._alive.append(obj)
        return handle, obj

    def _crypt(self, handle, data, iv, decrypt=False):
        bcrypt = self.bcrypt
        out = ctypes.create_string_buffer(len(data))
        result = ctypes.c_ulong()
        iv_buf = ctypes.create_string_buffer(iv, 16) if iv is not None else None
        iv_ptr = ctypes.cast(iv_buf, ctypes.c_void_p) if iv_buf is not None else None
        iv_len = 16 if iv_buf is not None else 0
        fn = bcrypt.BCryptDecrypt if decrypt else bcrypt.BCryptEncrypt
        status = fn(
            handle, data, len(data), None, iv_ptr, iv_len,
            out, len(data), ctypes.byref(result), 0)
        if status != 0:
            raise MegaError(f'Windows AES failed ({status})')
        return out.raw[:result.value]

    def ecb_encrypt(self, data):
        return self._crypt(self.h_ecb, data, None, False)

    def ecb_decrypt(self, data):
        return self._crypt(self.h_ecb, data, None, True)

    def cbc_encrypt(self, iv, data):
        if not data:
            return b''
        return self._crypt(self.h_cbc, data, iv, False)

    def cbc_decrypt(self, iv, data):
        if not data:
            return b''
        return self._crypt(self.h_cbc, data, iv, True)

    def ctr_xor(self, counter, data):
        if not data:
            return b'', counter
        blocks = (len(data) + 15) // 16
        stream = self.ecb_encrypt(_counter_blocks(counter, blocks))
        return _xor_bytes(data, stream, len(data)), (counter + blocks) & _MASK128

    def close(self):
        try:
            if self.h_ecb.value:
                self.bcrypt.BCryptDestroyKey(self.h_ecb)
            if self.h_cbc.value:
                self.bcrypt.BCryptDestroyKey(self.h_cbc)
        except Exception:
            pass


_BACKEND = None
_BCRYPT_ALGS = None
_OPENSSL = None
_SLOW_WARNED = False


def _try_cryptodome():
    from Crypto.Cipher import AES
    from Crypto.Util import Counter
    AES.Counter = Counter
    return 'cryptodome', AES


def _try_cryptography():
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    lib = type('C', (), {})()
    lib.Cipher = Cipher
    lib.algorithms = algorithms
    lib.modes = modes
    return 'cryptography', lib


def _try_openssl():
    import ctypes
    import ctypes.util
    global _OPENSSL
    if _OPENSSL:
        return 'openssl', _OPENSSL
    names = []
    if os.name == 'nt':
        names = ['libcrypto-3-x64.dll', 'libcrypto-3.dll', 'libcrypto.dll']
    else:
        found = ctypes.util.find_library('crypto')
        names = [found, 'libcrypto.so.3', 'libcrypto.so.1.1']
    lib = None
    for name in names:
        if not name:
            continue
        try:
            lib = ctypes.CDLL(name)
            break
        except Exception:
            lib = None
    if lib is None:
        raise MegaError('OpenSSL not found')
    lib.EVP_CIPHER_CTX_new.restype = ctypes.c_void_p
    lib.EVP_CIPHER_CTX_new.argtypes = []
    lib.EVP_CIPHER_CTX_free.argtypes = [ctypes.c_void_p]
    for name in ('EVP_aes_128_ecb', 'EVP_aes_128_cbc', 'EVP_aes_128_ctr'):
        fn = getattr(lib, name)
        fn.restype = ctypes.c_void_p
        fn.argtypes = []
    for name in ('EVP_EncryptInit_ex', 'EVP_DecryptInit_ex'):
        fn = getattr(lib, name)
        fn.restype = ctypes.c_int
        fn.argtypes = [
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
            ctypes.c_char_p, ctypes.c_char_p]
    for name in ('EVP_EncryptUpdate', 'EVP_DecryptUpdate'):
        fn = getattr(lib, name)
        fn.restype = ctypes.c_int
        fn.argtypes = [
            ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_int),
            ctypes.c_char_p, ctypes.c_int]
    for name in ('EVP_EncryptFinal_ex', 'EVP_DecryptFinal_ex'):
        fn = getattr(lib, name)
        fn.restype = ctypes.c_int
        fn.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_int)]
    lib.EVP_CIPHER_CTX_set_padding.argtypes = [ctypes.c_void_p, ctypes.c_int]
    lib.EVP_CIPHER_CTX_set_padding.restype = ctypes.c_int
    _OPENSSL = lib
    return 'openssl', lib


def _try_bcrypt():
    import ctypes
    from ctypes import wintypes
    global _BCRYPT_ALGS
    if os.name != 'nt':
        raise MegaError('not Windows')
    if _BCRYPT_ALGS:
        return 'bcrypt', _BCRYPT_ALGS
    bcrypt = ctypes.WinDLL('bcrypt.dll')
    ntstatus = ctypes.c_long
    bcrypt.BCryptOpenAlgorithmProvider.argtypes = [
        ctypes.POINTER(ctypes.c_void_p), wintypes.LPCWSTR,
        wintypes.LPCWSTR, wintypes.ULONG]
    bcrypt.BCryptOpenAlgorithmProvider.restype = ntstatus
    bcrypt.BCryptSetProperty.argtypes = [
        ctypes.c_void_p, wintypes.LPCWSTR, ctypes.c_void_p,
        wintypes.ULONG, wintypes.ULONG]
    bcrypt.BCryptSetProperty.restype = ntstatus
    bcrypt.BCryptGetProperty.argtypes = [
        ctypes.c_void_p, wintypes.LPCWSTR, ctypes.c_void_p,
        wintypes.ULONG, ctypes.POINTER(wintypes.ULONG), wintypes.ULONG]
    bcrypt.BCryptGetProperty.restype = ntstatus
    bcrypt.BCryptGenerateSymmetricKey.argtypes = [
        ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p,
        wintypes.ULONG, ctypes.c_void_p, wintypes.ULONG, wintypes.ULONG]
    bcrypt.BCryptGenerateSymmetricKey.restype = ntstatus
    for name in ('BCryptEncrypt', 'BCryptDecrypt'):
        fn = getattr(bcrypt, name)
        fn.argtypes = [
            ctypes.c_void_p, ctypes.c_void_p, wintypes.ULONG, ctypes.c_void_p,
            ctypes.c_void_p, wintypes.ULONG, ctypes.c_void_p, wintypes.ULONG,
            ctypes.POINTER(wintypes.ULONG), wintypes.ULONG]
        fn.restype = ntstatus
    bcrypt.BCryptDestroyKey.argtypes = [ctypes.c_void_p]
    bcrypt.BCryptDestroyKey.restype = ntstatus
    bcrypt.BCryptCloseAlgorithmProvider.argtypes = [ctypes.c_void_p, wintypes.ULONG]
    bcrypt.BCryptCloseAlgorithmProvider.restype = ntstatus

    def _open(mode):
        handle = ctypes.c_void_p()
        status = bcrypt.BCryptOpenAlgorithmProvider(
            ctypes.byref(handle), 'AES', None, 0)
        if status != 0 or not handle.value:
            raise MegaError(f'Windows AES unavailable ({status})')
        mode_buf = ctypes.create_unicode_buffer(mode)
        status = bcrypt.BCryptSetProperty(
            handle, 'ChainingMode', ctypes.addressof(mode_buf),
            ctypes.sizeof(mode_buf), 0)
        if status != 0:
            raise MegaError(f'Windows AES mode {mode} failed ({status})')
        return handle, mode_buf

    alg_ecb, keep_ecb = _open('ChainingModeECB')
    alg_cbc, keep_cbc = _open('ChainingModeCBC')
    _BCRYPT_ALGS = (bcrypt, alg_ecb, alg_cbc, keep_ecb, keep_cbc)
    return 'bcrypt', _BCRYPT_ALGS


def _backend():
    global _BACKEND
    if _BACKEND:
        return _BACKEND
    attempts = (
        _try_cryptodome,
        _try_cryptography,
        _try_openssl,
        _try_bcrypt,
    )
    for attempt in attempts:
        try:
            name, lib = attempt()
            probe = _cipher_from(name, lib, _FIPS_KEY)
            py = _PythonCipher(_FIPS_KEY)
            try:
                got = probe.ecb_encrypt(_FIPS_PLAIN)
                sample = _FIPS_PLAIN * 2
                if probe.cbc_encrypt(b'\0' * 16, sample) != py.cbc_encrypt(b'\0' * 16, sample):
                    raise MegaError(f'{name} CBC did not match')
                ctr_in = sample + b'partial'
                ctr_counter = 0x0102030405060708 << 64
                if probe.ctr_xor(ctr_counter, ctr_in)[0] != py.ctr_xor(ctr_counter, ctr_in)[0]:
                    raise MegaError(f'{name} CTR did not match')
            finally:
                probe.close()
            if got != _FIPS_CIPHER:
                raise MegaError(f'{name} AES did not match the test vector')
            _BACKEND = (name, lib)
            print(f'[MEGA] AES backend: {name}', flush=True)
            return _BACKEND
        except Exception:
            continue
    _BACKEND = ('python', None)
    print('[MEGA] AES backend: python', flush=True)
    return _BACKEND


def _cipher_from(name, lib, key):
    if name == 'python':
        return _PythonCipher(key)
    if name in ('cryptodome', 'cryptography'):
        return _LibraryCipher(key, name, lib)
    if name == 'openssl':
        return _OpenSslCipher(key, lib)
    if name == 'bcrypt':
        bcrypt, alg_ecb, alg_cbc = lib[:3]
        return _BcryptCipher(key, bcrypt, alg_ecb, alg_cbc)
    raise MegaError(f'unknown AES backend {name}')


def _cipher(key):
    name, lib = _backend()
    return _cipher_from(name, lib, key)


def aes_backend_name():
    return _backend()[0]


def _warn_slow_once():
    global _SLOW_WARNED
    if _SLOW_WARNED or aes_backend_name() != 'python':
        return
    _SLOW_WARNED = True
    print('[MEGA] decrypting without a fast AES library, so a long file will '
          'be slow. pip install pycryptodome makes this much faster.',
          flush=True)


def _cbc_decrypt(data, key, iv=b'\0' * 16):
    cipher = _cipher(key)
    try:
        return cipher.cbc_decrypt(iv, data)
    finally:
        cipher.close()


def _cbc_encrypt(data, key, iv):
    cipher = _cipher(key)
    try:
        return cipher.cbc_encrypt(iv, data)
    finally:
        cipher.close()


def split_file_key(file_key):
    """8 big-endian ints from the URL key → (aes_key_ints, iv, meta_mac)."""
    if len(file_key) < 8:
        raise MegaError('MEGA key is too short to decrypt this file')
    k = (
        file_key[0] ^ file_key[4],
        file_key[1] ^ file_key[5],
        file_key[2] ^ file_key[6],
        file_key[3] ^ file_key[7],
    )
    iv = (file_key[4], file_key[5], 0, 0)
    meta_mac = (file_key[6], file_key[7])
    return k, iv, meta_mac


def decrypt_key(encrypted, key):
    """Unwrap a node key. Each 16-byte block is CBC with a zero IV."""
    raw = _a32_to_bytes(encrypted)
    key_bytes = _a32_to_bytes(key[:4])
    out = bytearray()
    for i in range(0, len(raw) - len(raw) % 16, 16):
        out.extend(_cbc_decrypt(raw[i:i + 16], key_bytes, b'\0' * 16))
    return _bytes_to_a32(bytes(out))


def decrypt_attr(attr, key):
    plain = _cbc_decrypt(attr, _a32_to_bytes(key[:4]), b'\0' * 16)
    text = plain.split(b'\0', 1)[0]
    for encoding in ('utf-8', 'latin-1'):
        try:
            decoded = text.decode(encoding)
        except Exception:
            continue
        if decoded[:6] != 'MEGA{"':
            continue
        try:
            return json.loads(decoded[4:])
        except Exception:
            continue
    raise MegaError(
        'MEGA link did not decrypt. If this file has a password, '
        'say so and I will add that.')


def _mega_chunks(size):
    position = 0
    chunk = 0x20000
    while position + chunk < size:
        yield position, chunk
        position += chunk
        if chunk < 0x100000:
            chunk += 0x20000
    if position < size:
        yield position, size - position


def _api(payload, node=None, timeout=20, attempts=3):
    last = None
    for attempt in range(max(1, attempts)):
        host = _API_HOSTS[attempt % len(_API_HOSTS)]
        params = {'id': str(random.randint(0, 0xFFFFFFFF))}
        if node:
            params['n'] = node
        url = host + '?' + urllib.parse.urlencode(params)
        body = json.dumps(payload if isinstance(payload, list) else [payload])
        req = urllib.request.Request(
            url, data=body.encode('utf-8'),
            headers={'Content-Type': 'application/json', 'User-Agent': _UA})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                parsed = json.loads(resp.read().decode('utf-8', errors='replace'))
        except urllib.error.HTTPError as exc:
            last = MegaError(f'MEGA API HTTP {exc.code}')
            continue
        except Exception as exc:
            last = MegaError(f'MEGA API unreachable: {exc}')
            continue
        item = parsed[0] if isinstance(parsed, list) and parsed else parsed
        if isinstance(item, int):
            if item == -3 and attempt < 4:
                continue
            raise MegaError(_ERRORS.get(item, f'MEGA error {item}'), item)
        if not isinstance(item, dict):
            raise MegaError('MEGA returned an unexpected reply')
        return item
    raise last or MegaError('MEGA API unreachable')


def _file_record(file_id, key_text, attrs, size):
    return {
        'id': file_id,
        'key': key_text,
        'name': str((attrs or {}).get('n') or file_id),
        'size': int(size or 0),
    }


def describe(link):
    """Name and size of one public file, without downloading it."""
    if link.kind == 'file':
        key_ints = _base64_to_a32(link.file_key)
        k, _iv, _mac = split_file_key(key_ints)
        data = _api({'a': 'g', 'p': link.file_id, 'ssl': 2})
        attrs = decrypt_attr(_base64_url_decode(data.get('at') or ''), k)
        return _file_record(link.file_id, link.file_key, attrs, data.get('s'))
    if link.kind == 'folder_file':
        files = list_folder(link.folder_id, link.folder_key)
        for item in files:
            if item['id'] == link.node_id:
                return item
        raise MegaError('That file is not in the MEGA folder')
    raise MegaError('That MEGA link is a folder, not one file')


def list_folder(folder_id, folder_key):
    share = _base64_to_a32(folder_key)
    if len(share) < 2:
        raise MegaError('MEGA folder key is too short')
    # A folder key is 128 bits. A longer value is a file key pasted by mistake.
    share = share[:4] if len(share) >= 4 else share
    if len(share) < 4:
        share = (share + [0, 0, 0, 0])[:4]
    data = _api({'a': 'f', 'c': 1, 'ca': 1, 'r': 1}, node=folder_id)
    nodes = data.get('f') if isinstance(data, dict) else None
    if not isinstance(nodes, list):
        raise MegaError('MEGA folder listing had no files')
    files = []
    folders = {}
    decoded = []
    for node in nodes:
        if not isinstance(node, dict):
            continue
        kind = int(node.get('t') if node.get('t') is not None else -1)
        key_field = str(node.get('k') or '')
        enc_text = key_field.split(':')[-1]
        if not enc_text:
            continue
        try:
            decrypted = decrypt_key(_base64_to_a32(enc_text), share)
        except Exception:
            continue
        decoded.append((node, kind, decrypted))
    for node, kind, decrypted in decoded:
        handle = str(node.get('h') or '')
        if kind == 1 and len(decrypted) >= 4:
            try:
                attrs = decrypt_attr(
                    _base64_url_decode(node.get('a') or ''), decrypted[:4])
                folders[handle] = str(attrs.get('n') or '')
            except Exception:
                folders[handle] = ''
    for node, kind, decrypted in decoded:
        if kind != 0 or len(decrypted) < 8:
            continue
        handle = str(node.get('h') or '')
        try:
            k, _iv, _mac = split_file_key(decrypted[:8])
            attrs = decrypt_attr(_base64_url_decode(node.get('a') or ''), k)
        except Exception:
            continue
        name = str(attrs.get('n') or handle)
        parent = str(node.get('p') or '')
        prefix = folders.get(parent) or ''
        if prefix and parent != folder_id:
            name = prefix + '/' + name
        files.append(_file_record(
            handle, _base64_url_encode(_a32_to_bytes(decrypted[:8])),
            {'n': name}, node.get('s')))
    files.sort(key=lambda item: str(item.get('name') or '').lower())
    return files


def _ext(name):
    return os.path.splitext(str(name or ''))[1].lower()


def playable_files(files):
    media = [item for item in files if _ext(item.get('name')) in _MEDIA_EXT]
    if media:
        return media
    others = [
        item for item in files
        if _ext(item.get('name')) not in _SUB_EXT
        and int(item.get('size') or 0) >= 1 << 20
    ]
    return others


def subtitle_files(files):
    return [item for item in files if _ext(item.get('name')) in _SUB_EXT]


def match_subtitle(video, subtitles):
    stem = os.path.splitext(os.path.basename(video.get('name') or ''))[0].lower()
    stem = re.sub(r'[\s._\-]+', '', stem)
    best = None
    for sub in subtitles or []:
        sub_stem = os.path.splitext(os.path.basename(sub.get('name') or ''))[0].lower()
        sub_stem = re.sub(r'[\s._\-]+', '', sub_stem)
        if not stem or not sub_stem:
            continue
        if stem.startswith(sub_stem) or sub_stem.startswith(stem):
            return sub
        if len(stem) >= 6 and stem[:6] == sub_stem[:6]:
            best = sub
    if best:
        return best
    if len(subtitles or []) == 1 and len(files_guard(subtitles)) == 1:
        return subtitles[0]
    return None


def files_guard(items):
    return items or []


def _safe_name(name, fallback):
    text = re.sub(r'[\\/:*?"<>|]+', ' ', str(name or '')).strip()
    text = re.sub(r'\s+', ' ', text).strip().strip('.')
    return text or fallback


def existing_download(dest_dir, file_id, size):
    marker = os.path.join(dest_dir, f'.{file_id}.path')
    try:
        with open(marker, encoding='utf-8') as fh:
            path = fh.read().strip()
    except Exception:
        return ''
    if path and os.path.isfile(path) and (not size or os.path.getsize(path) == int(size)):
        return path
    return ''


def _remember_download(dest_dir, file_id, path):
    try:
        os.makedirs(dest_dir, exist_ok=True)
        with open(os.path.join(dest_dir, f'.{file_id}.path'), 'w', encoding='utf-8') as fh:
            fh.write(path)
    except Exception:
        pass


def _mac_chunk(cipher, chunk, iv_str, mac_state, file_size):
    """MEGA's chunk MAC: CBC from iv||iv, then fold the last block."""
    if not chunk:
        return mac_state
    if file_size > 16 and len(chunk) > 16:
        tail = len(chunk) % 16 or 16
        split = len(chunk) - tail
        prefix = chunk[:split]
        final = chunk[split:split + 16]
    else:
        prefix = b''
        final = chunk[:16]
    state = iv_str
    if prefix:
        encrypted = cipher.cbc_encrypt(iv_str, prefix)
        if len(encrypted) >= 16:
            state = encrypted[-16:]
    if len(final) % 16:
        final += b'\0' * (16 - len(final) % 16)
    if len(final) < 16:
        return mac_state
    folded = cipher.cbc_encrypt(state, final)
    return cipher.cbc_encrypt(mac_state, folded)


def download_file(file_id, file_key, dest_dir, filename='', progress=None,
                  cancel=None, folder_id='', dest_name=''):
    """Decrypt a public file into dest_dir. Returns the local path."""
    _warn_slow_once()
    key_ints = _base64_to_a32(file_key)
    k, iv, meta_mac = split_file_key(key_ints)
    if folder_id:
        data = _api({'a': 'g', 'g': 1, 'n': file_id, 'ssl': 2}, node=folder_id)
    else:
        data = _api({'a': 'g', 'g': 1, 'p': file_id, 'ssl': 2})
    url = str(data.get('g') or '')
    if not url:
        raise MegaError('MEGA did not give a download URL for this file')
    size = int(data.get('s') or 0)
    if size <= 0:
        raise MegaError('MEGA reported an empty file')
    try:
        attrs = decrypt_attr(_base64_url_decode(data.get('at') or ''), k)
        name = attrs.get('n') or filename or file_id
    except MegaError:
        raise
    except Exception:
        name = filename or file_id
    cached = existing_download(dest_dir, file_id, size)
    if cached:
        if progress:
            progress(size, size)
        return cached
    os.makedirs(dest_dir, exist_ok=True)
    filename = _safe_name(dest_name or name, file_id)
    final_path = os.path.join(dest_dir, filename)
    if os.path.exists(final_path):
        base, ext = os.path.splitext(final_path)
        index = 2
        while os.path.exists(f'{base} ({index}){ext}'):
            index += 1
        final_path = f'{base} ({index}){ext}'
    part_path = final_path + '.part'
    key_bytes = _a32_to_bytes(k)
    counter = ((int(iv[0]) << 32) + int(iv[1])) << 64
    mac_iv = _a32_to_bytes((iv[0], iv[1], iv[0], iv[1]))
    mac_state = b'\0' * 16
    print(f'[MEGA] downloading {filename} ({format_size(size)}) '
          f'via {aes_backend_name()}', flush=True)
    req = urllib.request.Request(url, headers={'User-Agent': _UA})
    done = 0
    cipher = _cipher(key_bytes)
    try:
        with urllib.request.urlopen(req, timeout=120) as resp, open(part_path, 'wb') as out:
            for _start, chunk_size in _mega_chunks(size):
                blob = b''
                while len(blob) < chunk_size:
                    if cancel and cancel():
                        raise MegaError('Download cancelled')
                    piece = resp.read(min(256 * 1024, chunk_size - len(blob)))
                    if not piece:
                        break
                    blob += piece
                if len(blob) < chunk_size:
                    raise MegaError(
                        f'MEGA download stopped at {format_size(done)} '
                        f'of {format_size(size)}')
                plain, counter = cipher.ctr_xor(counter, blob)
                if len(plain) != len(blob):
                    raise MegaError('MEGA decrypt returned the wrong size')
                out.write(plain)
                mac_state = _mac_chunk(cipher, plain, mac_iv, mac_state, size)
                done += len(plain)
                if progress:
                    progress(done, size)
    except MegaError:
        try:
            os.remove(part_path)
        except Exception:
            pass
        raise
    except Exception as exc:
        try:
            os.remove(part_path)
        except Exception:
            pass
        raise MegaError(f'MEGA download failed: {exc}') from exc
    finally:
        cipher.close()
    file_mac = _bytes_to_a32(mac_state)
    got = (file_mac[0] ^ file_mac[1], file_mac[2] ^ file_mac[3])
    if got != meta_mac:
        print('[MEGA] decryption check did not match — playing anyway '
              'if the file looks like media', flush=True)
        with open(part_path, 'rb') as fh:
            head = fh.read(16)
        if not _looks_like_media(head, filename):
            try:
                os.remove(part_path)
            except Exception:
                pass
            raise MegaError(
                'MEGA file did not decrypt. The link key may be wrong.')
    os.replace(part_path, final_path)
    _remember_download(dest_dir, file_id, final_path)
    print(f'[MEGA] saved {final_path}', flush=True)
    return final_path


def _looks_like_media(head, filename):
    if _ext(filename) in _MEDIA_EXT or _ext(filename) in _SUB_EXT:
        return True
    if len(head) >= 8 and head[4:8] == b'ftyp':
        return True
    if head.startswith(b'\x1aE\xdf\xa3'):
        return True
    if head.startswith((b'ID3', b'RIFF', b'OggS', b'FLV', b'\x00\x00\x01')):
        return True
    return False


_FETCH_BACKEND = ''


def _fetch_range(url, start, end):
    """Read encrypted bytes start..end inclusive from a MEGA storage URL.

    Tries a browser TLS fingerprint first. The storage nodes reset a plain
    Python connection (WinError 10054) even when the API call itself works.
    A 200 that ignores Range is cut off after the requested length so a
    seek never turns into a full-file download.
    """
    want = end - start + 1
    if want <= 0:
        return 206, b'', ''
    headers = {
        'User-Agent': _UA,
        'Accept': '*/*',
        'Accept-Encoding': 'identity',
        'Referer': 'https://mega.nz/',
        'Origin': 'https://mega.nz',
        'Range': f'bytes={start}-{end}',
        'Connection': 'close',
    }
    errors = []

    def _take(status, iterator, content_range, close):
        buf = bytearray()
        try:
            for chunk in iterator:
                if not chunk:
                    continue
                buf.extend(chunk)
                if len(buf) >= want:
                    break
        finally:
            close()
        return int(status), bytes(buf[:want]), str(content_range or '')

    global _FETCH_BACKEND
    try:
        import curl_cffi.requests as cfreq
        for impersonate in ('chrome131', 'chrome124'):
            try:
                response = cfreq.get(
                    url, headers=headers, impersonate=impersonate,
                    timeout=20, allow_redirects=True, stream=True)
            except Exception as exc:
                errors.append(f'{impersonate}: {exc}')
                continue
            status = int(getattr(response, 'status_code', 0) or 0)
            if status in (200, 206):
                _FETCH_BACKEND = 'curl_cffi'
                return _take(
                    status, response.iter_content(65536),
                    response.headers.get('Content-Range'), response.close)
            errors.append(f'{impersonate}: HTTP {status}')
            try:
                response.close()
            except Exception:
                pass
    except ImportError:
        errors.append('curl_cffi not installed')

    try:
        import requests
        response = requests.get(
            url, headers=headers, timeout=20, allow_redirects=True, stream=True)
        status = int(response.status_code or 0)
        if status in (200, 206):
            _FETCH_BACKEND = 'requests'
            return _take(
                status, response.iter_content(65536),
                response.headers.get('Content-Range'), response.close)
        errors.append(f'requests: HTTP {status}')
        response.close()
    except ImportError:
        errors.append('requests not installed')
    except Exception as exc:
        errors.append(f'requests: {exc}')

    req = urllib.request.Request(url, headers=headers)
    try:
        resp = urllib.request.urlopen(req, timeout=20)
    except Exception as exc:
        errors.append(f'urllib: {exc}')
        detail = '; '.join(errors[-3:])
        hint = ''
        if any('curl_cffi not installed' in item for item in errors):
            hint = ' Install curl_cffi (pip install curl_cffi) and try the link again.'
        raise MegaError('MEGA closed the storage connection (' + detail + ').' + hint)
    _FETCH_BACKEND = 'urllib'

    def _iter():
        while True:
            piece = resp.read(65536)
            if not piece:
                break
            yield piece

    return _take(getattr(resp, 'status', 200), _iter(), resp.headers.get('Content-Range'), resp.close)


class MegaStream(object):
    """One public file, decrypted in whatever range the player asks for."""

    def __init__(self, file_id, file_key, folder_id='', title=''):
        self.file_id = file_id
        self.file_key = file_key
        self.folder_id = folder_id or ''
        self.title = title or ''
        self.url = ''
        self.size = 0
        self.name = title or file_id
        self.key_bytes = b''
        self.counter0 = 0
        self.content_type = 'application/octet-stream'
        self.refreshed_at = 0.0
        self.logged = False
        self.lock = threading.Lock()
        self._blocks = {}
        self._inflight = {}
        self._block_lock = threading.Lock()
        self._cache_bytes = 0
        # MEGA often allows one storage connection. A seek must wait for the
        # chunk already in flight, not open a second one and get reset.
        self.fetch_lock = threading.Lock()
        self.last_request_start = None
        self.refresh(force=True)
        threading.Thread(
            target=self._prefetch_index, name='mega-index', daemon=True).start()

    def refresh(self, force=False):
        with self.lock:
            # Coalesce the parallel range requests mpv opens. A dead URL is
            # refreshed, but not on every retry in the same second.
            if self.url and (time.time() - self.refreshed_at) < (1 if force else 3):
                return
            if self.folder_id:
                data = _api(
                    {'a': 'g', 'g': 1, 'n': self.file_id, 'ssl': 2},
                    node=self.folder_id)
            else:
                data = _api({'a': 'g', 'g': 1, 'p': self.file_id, 'ssl': 2})
            url = str(data.get('g') or '')
            if not url:
                raise MegaError('MEGA did not give a download URL for this file')
            size = int(data.get('s') or 0)
            if size <= 0:
                raise MegaError('MEGA reported an empty file')
            key_ints = _base64_to_a32(self.file_key)
            k, iv, _mac = split_file_key(key_ints)
            name = self.title or self.file_id
            try:
                attrs = decrypt_attr(_base64_url_decode(data.get('at') or ''), k)
                name = attrs.get('n') or name
            except Exception:
                pass
            self.url = url
            self.size = size
            self.name = _safe_name(name, self.file_id)
            self.key_bytes = _a32_to_bytes(k)
            self.counter0 = ((int(iv[0]) << 32) + int(iv[1])) << 64
            self.content_type = _content_type(self.name)
            self.refreshed_at = time.time()
            if force:
                print(f'[MEGA] streaming {self.name} ({format_size(self.size)}) '
                      f'— not saving the file', flush=True)

    def _prefetch_index(self):
        """Warm the MP4 index. A time-bar jump needs it, and it sits at the end.

        Wait so the playhead's first chunks are not stuck behind this fetch.
        """
        time.sleep(2)
        try:
            tail = min(2 * 1024 * 1024, self.size)
            if tail > 0:
                self.read(self.size - tail, tail)
        except Exception as exc:
            print(f'[MEGA] index prefetch failed: {exc}', flush=True)

    def read(self, start, length):
        if start >= self.size or length <= 0:
            return b''
        length = min(int(length), self.size - int(start))
        out = bytearray()
        pos = int(start)
        end = pos + length
        while pos < end:
            index = pos // _BLOCK
            block = self._block(index)
            if not block:
                break
            offset = pos - index * _BLOCK
            if offset >= len(block):
                break
            take = min(len(block) - offset, end - pos)
            out.extend(block[offset:offset + take])
            pos += take
        return bytes(out)

    def _block(self, index):
        with self._block_lock:
            cached = self._blocks.get(index)
            if cached is not None:
                return cached
            waiter = self._inflight.get(index)
            if waiter is None:
                waiter = threading.Event()
                self._inflight[index] = waiter
                owner = True
            else:
                owner = False
        if not owner:
            waiter.wait(90)
            with self._block_lock:
                return self._blocks.get(index, b'')
        try:
            start = index * _BLOCK
            end = min(self.size, start + _BLOCK) - 1
            encrypted = self._get(start, end)
            skip = 0
            cipher = _cipher(self.key_bytes)
            try:
                plain, _next = cipher.ctr_xor(self.counter0 + index * (_BLOCK // 16), encrypted)
            finally:
                cipher.close()
            # The block is 16-byte aligned, so the plaintext is the file bytes.
            data = plain[:end - start + 1]
            with self._block_lock:
                self._blocks[index] = data
                self._cache_bytes += len(data)
                self._evict_blocks()
            return data
        finally:
            with self._block_lock:
                self._inflight.pop(index, None)
            waiter.set()

    def _evict_blocks(self):
        # Keep the start and the index at the end. Those are what a seek re-reads.
        tail = max(0, (self.size - 1) // _BLOCK - 4)
        while self._cache_bytes > 96 * 1024 * 1024 and len(self._blocks) > 8:
            victim = None
            for index in list(self._blocks):
                if index == 0 or index >= tail:
                    continue
                victim = index
                break
            if victim is None:
                break
            removed = self._blocks.pop(victim, b'')
            self._cache_bytes -= len(removed)

    def _get(self, start, end):
        """Encrypted bytes start..end inclusive. Retries a short or reset read."""
        want = end - start + 1
        buf = bytearray()
        pos = int(start)
        last = None
        attempt = 0
        while len(buf) < want and attempt < 8:
            piece_end = min(end, pos + _BLOCK - 1)
            try:
                with self.fetch_lock:
                    status, data, content_range = _fetch_range(self.url, pos, piece_end)
            except Exception as exc:
                last = exc if isinstance(exc, MegaError) else MegaError(
                    f'MEGA closed the storage connection ({exc})')
                attempt += 1
                try:
                    self.refresh(force=True)
                except Exception:
                    pass
                time.sleep(min(2.0, 0.25 * attempt))
                continue
            ranged_ok = _content_range_matches(content_range, pos)
            if content_range and not ranged_ok:
                usable = False
            else:
                usable = status == 206 or (status == 200 and pos == 0) or ranged_ok
            if not usable or not data:
                last = MegaError(
                    'MEGA storage ignored the range request'
                    if status == 200 else f'MEGA storage HTTP {status}')
                if status in (200, 403, 429, 500, 502, 503, 509) or not data:
                    try:
                        self.refresh(force=True)
                    except Exception as exc:
                        last = exc
                attempt += 1
                if attempt == 1 or attempt == 4:
                    print(f'[MEGA] range {pos}-{piece_end} failed: {last}', flush=True)
                time.sleep(min(2.0, 0.25 * attempt))
                continue
            if not self.logged:
                self.logged = True
                print(f'[MEGA] storage connected via {_FETCH_BACKEND or "urllib"} '
                      f'(HTTP {status})', flush=True)
            buf.extend(data)
            pos += len(data)
            if len(data) < (piece_end - (pos - len(data)) + 1):
                # Short 206: keep the bytes and ask for the rest. Not a failure.
                continue
            attempt = 0
        if len(buf) < want:
            raise last or MegaError('MEGA closed the storage connection')
        return bytes(buf[:want])


def _content_type(name):
    ext = _ext(name)
    return {
        '.mp4': 'video/mp4',
        '.m4v': 'video/mp4',
        '.mkv': 'video/x-matroska',
        '.webm': 'video/webm',
        '.mov': 'video/quicktime',
        '.avi': 'video/x-msvideo',
        '.ts': 'video/mp2t',
        '.mp3': 'audio/mpeg',
        '.m4a': 'audio/mp4',
        '.flac': 'audio/flac',
    }.get(ext, 'application/octet-stream')


_BLOCK = 1024 * 1024


def _content_range_matches(header, start):
    text = str(header or '').strip().lower()
    if not text.startswith('bytes'):
        return False
    try:
        return int(text.split(' ', 1)[1].split('-', 1)[0]) == int(start)
    except Exception:
        return False


_PROXY_LOCK = threading.Lock()
_PROXY_SERVER = None
_PROXY_PORT = 0
_STREAMS = {}


class _MegaProxyHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, fmt, *args):
        return

    def do_HEAD(self):
        self._serve(head_only=True)

    def do_GET(self):
        self._serve(head_only=False)

    def _stream(self):
        parts = [part for part in self.path.split('?')[0].split('/') if part]
        if len(parts) < 2 or parts[0] != 'mega':
            return None
        return _STREAMS.get(parts[1])

    def _serve(self, head_only):
        stream = self._stream()
        if stream is None:
            self.send_error(404, 'Unknown MEGA stream')
            return
        span = _parse_range(self.headers.get('Range'), stream.size)
        if span is None:
            self.send_response(416)
            self.send_header('Content-Range', f'bytes */{stream.size}')
            self.send_header('Accept-Ranges', 'bytes')
            self.end_headers()
            return
        start, end, ranged = span
        length = end - start + 1
        previous = getattr(stream, 'last_request_start', None)
        try:
            stream.last_request_start = start
        except Exception:
            pass
        if previous is None or abs(start - previous) > 2 * 1024 * 1024:
            print(f'[MEGA] player range {start}-{end}', flush=True)
            if previous is not None and hasattr(stream, 'refresh'):
                try:
                    stream.refresh(force=True)
                except Exception as exc:
                    print(f'[MEGA] refresh before seek failed: {exc}', flush=True)
        self.close_connection = True
        first = b''
        if not head_only:
            first = self._read_chunk(stream, start, min(_BLOCK, length))
            if not first:
                self.send_error(502, 'MEGA storage connection failed')
                return
        self.send_response(206 if ranged else 200)
        self.send_header('Content-Type', stream.content_type)
        self.send_header('Accept-Ranges', 'bytes')
        self.send_header('Content-Length', str(length))
        if ranged:
            self.send_header('Content-Range', f'bytes {start}-{end}/{stream.size}')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Connection', 'close')
        self.end_headers()
        if head_only:
            return
        try:
            self.wfile.write(first)
        except Exception:
            return
        pos = start + len(first)
        while pos <= end:
            chunk = self._read_chunk(stream, pos, min(_BLOCK, end - pos + 1))
            if not chunk:
                return
            try:
                self.wfile.write(chunk)
            except Exception:
                return
            pos += len(chunk)

    def _read_chunk(self, stream, pos, length):
        error = None
        for attempt in range(6):
            try:
                chunk = stream.read(pos, length)
            except Exception as exc:
                error = exc
                chunk = b''
            if chunk:
                return chunk
            print(f'[MEGA] read retry {attempt + 1} at {pos}: {error or "empty"}', flush=True)
            time.sleep(min(1.5, 0.2 * (attempt + 1)))
        print(f'[MEGA] player range ended early at {pos}: {error}', flush=True)
        return b''


def _parse_range(header, size):
    if not header:
        return 0, max(0, size - 1), False
    text = str(header).strip()
    if not text.lower().startswith('bytes='):
        return 0, max(0, size - 1), False
    spec = text.split('=', 1)[1].split(',', 1)[0].strip()
    start_s, _, end_s = spec.partition('-')
    try:
        if start_s == '':
            count = int(end_s)
            start = max(0, size - count)
            end = size - 1
        else:
            start = int(start_s)
            end = int(end_s) if end_s else size - 1
    except Exception:
        return None
    if start < 0 or start >= size or end < start:
        return None
    return start, min(end, size - 1), True


def _ensure_proxy():
    global _PROXY_SERVER, _PROXY_PORT
    with _PROXY_LOCK:
        if _PROXY_SERVER is not None:
            return _PROXY_PORT
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        finally:
            sock.close()
        server = http.server.ThreadingHTTPServer(('127.0.0.1', port), _MegaProxyHandler)
        server.daemon_threads = True
        thread = threading.Thread(
            target=server.serve_forever, name='mega-stream-proxy', daemon=True)
        thread.start()
        _PROXY_SERVER = server
        _PROXY_PORT = port
        print(f'[MEGA] stream proxy on http://127.0.0.1:{port}', flush=True)
        return port


def stream_playback_url(file_id='', file_key='', folder_id='', title='', source_url=''):
    """Return a local URL mpv can play. Nothing is written to disk."""
    if source_url and (not file_id or not file_key):
        link = parse_mega_url(source_url)
        if link and link.kind == 'file':
            file_id = file_id or link.file_id
            file_key = file_key or link.file_key
        elif link and link.kind == 'folder_file':
            file_id = file_id or link.node_id
            folder_id = folder_id or link.folder_id
            if not file_key:
                rec = describe(link)
                file_id = rec.get('id') or file_id
                file_key = rec.get('key') or ''
                title = title or rec.get('name') or ''
    if not file_id or not file_key:
        raise MegaError('MEGA link is missing its decryption key')
    key = (file_id, folder_id or '')
    stream = _STREAMS.get(key)
    if stream is None or stream.file_key != file_key:
        stream = MegaStream(file_id, file_key, folder_id, title)
        token = secrets.token_urlsafe(12)
        _STREAMS[key] = stream
        _STREAMS[token] = stream
        stream.token = token
    if getattr(stream, 'playback_url', ''):
        return stream.playback_url
    port = _ensure_proxy()
    # The path has to end in the real extension. Otherwise mpv treats the
    # local URL as a page and turns yt-dlp back on.
    name = urllib.parse.quote(stream.name)
    if not _ext(stream.name):
        name += '.mp4'
    stream.playback_url = f'http://127.0.0.1:{port}/mega/{stream.token}/{name}'
    return stream.playback_url


def self_test():
    """AES vector, URL parse, and a MEGA-shaped encrypt/decrypt round trip."""
    link = parse_mega_url(
        'https://mega.nz/file/iu4wWThJ#1hdCj2-Dwg7SZOVYXI7uZxISIynVapzVFceAe5NoaRs')
    assert link and link.kind == 'file' and link.file_id == 'iu4wWThJ'
    assert len(_base64_url_decode(link.file_key)) == 32
    folder = parse_mega_url(
        'https://mega.nz/folder/jjJRGCwS#TawrhF6DslhH5oOb7kLecw/file/bnwmwZ7K')
    assert folder and folder.kind == 'folder_file' and folder.node_id == 'bnwmwZ7K'
    assert folder_file_url(folder.folder_id, folder.folder_key, folder.node_id)
    old = parse_mega_url(
        'https://mega.nz/#!iu4wWThJ!1hdCj2-Dwg7SZOVYXI7uZxISIynVapzVFceAe5NoaRs')
    assert old and old.kind == 'file' and old.file_id == 'iu4wWThJ'
    cipher = _cipher(_FIPS_KEY)
    try:
        assert cipher.ecb_encrypt(_FIPS_PLAIN) == _FIPS_CIPHER
        assert cipher.ecb_decrypt(_FIPS_CIPHER) == _FIPS_PLAIN
        body = os.urandom(3000)
        counter = ((0x11223344 << 32) + 0xaabbccdd) << 64
        blob, _next = cipher.ctr_xor(counter, body)
        back, _ = cipher.ctr_xor(counter, blob)
        assert back == body
        # Chunked CTR must continue the counter, including a short tail.
        # MEGA chunks are 16-byte aligned until the final tail. Continuing
        # a CTR call on an unaligned split would skip leftover keystream.
        pieces = []
        pos = 0
        running = counter
        for size in (256, 512, 1024, 1208):
            part, running = cipher.ctr_xor(running, body[pos:pos + size])
            pieces.append(part)
            pos += size
        assert b''.join(pieces) == blob
        # A seek must decrypt from the block that contains the offset, not
        # from the start of the file.
        start, length = 100, 50
        skip = start % 16
        aligned = blob[start - skip:start - skip + skip + length]
        plain, _ = cipher.ctr_xor(counter + ((start - skip) // 16), aligned)
        assert plain[skip:skip + length] == body[start:start + length]
    finally:
        cipher.close()
    _proxy_self_test()
    return aes_backend_name()


def _proxy_self_test():
    """The local player URL must answer a range without reading past it."""
    class _Fake(object):
        size = 1000
        name = 'clip.mp4'
        content_type = 'video/mp4'
        logged = True

        def read(self, start, length):
            length = min(length, self.size - start)
            return bytes((start + i) & 0xff for i in range(length))

    token = 'selftest'
    _STREAMS[token] = _Fake()
    port = _ensure_proxy()
    url = f'http://127.0.0.1:{port}/mega/{token}/clip.mp4'
    req = urllib.request.Request(url, headers={'Range': 'bytes=100-149'})
    with urllib.request.urlopen(req, timeout=5) as resp:
        assert resp.status == 206
        body = resp.read()
    assert body == bytes((100 + i) & 0xff for i in range(50))
    assert resp.headers.get('Content-Range') == 'bytes 100-149/1000'

    req = urllib.request.Request(url, headers={'Range': 'bytes=800-'})
    with urllib.request.urlopen(req, timeout=5) as resp:
        assert resp.status == 206
        body = resp.read()
    assert body == bytes((800 + i) & 0xff for i in range(200))
    assert resp.headers.get('Content-Range') == 'bytes 800-999/1000'
