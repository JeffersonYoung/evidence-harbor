"""Deterministic fixtures; no remote services are needed for the local test suite."""
from hashlib import sha256


def digest(value: str | bytes) -> str:
    return sha256(value.encode('utf-8') if isinstance(value, str) else value).hexdigest()


def text_pdf(*pages: str) -> bytes:
    """Make a valid small PDF with actual text content, without optional OCR tooling."""
    objects: list[bytes] = [b'<< /Type /Catalog /Pages 2 0 R >>', b'',
        b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>']
    page_ids: list[int] = []
    for text in pages:
        page_id = len(objects) + 1
        content_id = page_id + 1
        page_ids.append(page_id)
        objects.append((f'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] '
            f'/Resources << /Font << /F1 3 0 R >> >> /Contents {content_id} 0 R >>').encode())
        escaped = text.replace('\\', '\\\\').replace('(', '\\(').replace(')', '\\)')
        stream = f'BT /F1 12 Tf 72 720 Td ({escaped}) Tj ET'.encode('latin-1')
        objects.append(b'<< /Length ' + str(len(stream)).encode() + b' >>\nstream\n' + stream + b'\nendstream')
    objects[1] = (f'<< /Type /Pages /Count {len(page_ids)} /Kids [' +
        ' '.join(f'{number} 0 R' for number in page_ids) + '] >>').encode()
    result = bytearray(b'%PDF-1.4\n%\xe2\xe3\xcf\xd3\n')
    offsets = [0]
    for number, obj in enumerate(objects, 1):
        offsets.append(len(result))
        result.extend(f'{number} 0 obj\n'.encode() + obj + b'\nendobj\n')
    xref_offset = len(result)
    result.extend(f'xref\n0 {len(objects) + 1}\n0000000000 65535 f \n'.encode())
    for offset in offsets[1:]:
        result.extend(f'{offset:010} 00000 n \n'.encode())
    result.extend((f'trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n'
                   f'startxref\n{xref_offset}\n%%EOF\n').encode())
    return bytes(result)
