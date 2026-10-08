#!/usr/bin/env python3
"""Shrink the textures inside a .glb (geometry, rig and animations are untouched).

usage: python3 tools/optimize_glb.py in.glb out.glb [base_w] [orm_w]
Re-encodes each embedded image as JPEG, downscaled to the given width (2:1 aspect kept).
"""
import io, json, struct, sys
from PIL import Image

src, dst = sys.argv[1], sys.argv[2]
base_w = int(sys.argv[3]) if len(sys.argv) > 3 else 2048
orm_w = int(sys.argv[4]) if len(sys.argv) > 4 else 1024
data = open(src, 'rb').read()
jl = struct.unpack_from('<I', data, 12)[0]
j = json.loads(data[20:20 + jl])
bin_off = 20 + jl + 8
bin_chunk = data[bin_off:]
views = j['bufferViews']
img_views = {im['bufferView'] for im in j['images']}
base_imgs = {t['source'] for m in j['materials'] if 'baseColorTexture' in m.get('pbrMetallicRoughness', {})
             for t in [j['textures'][m['pbrMetallicRoughness']['baseColorTexture']['index']]]}
out = bytearray()
new_views = []
for i, v in enumerate(views):
    raw = bin_chunk[v.get('byteOffset', 0):v.get('byteOffset', 0) + v['byteLength']]
    if i in img_views:
        idx = next(k for k, im in enumerate(j['images']) if im['bufferView'] == i)
        w = base_w if idx in base_imgs else orm_w
        im = Image.open(io.BytesIO(raw)).convert('RGB')
        im = im.resize((w, max(1, round(im.height * w / im.width))), Image.LANCZOS)
        buf = io.BytesIO(); im.save(buf, 'JPEG', quality=86, optimize=True)
        raw = buf.getvalue()
        j['images'][idx]['mimeType'] = 'image/jpeg'
    while len(out) % 4: out.append(0)
    nv = dict(v); nv['byteOffset'] = len(out); nv['byteLength'] = len(raw)
    new_views.append(nv); out += raw
while len(out) % 4: out.append(0)
j['bufferViews'] = new_views
j['buffers'][0]['byteLength'] = len(out)
js = json.dumps(j, separators=(',', ':')).encode()
js += b' ' * (-len(js) % 4)
total = 12 + 8 + len(js) + 8 + len(out)
with open(dst, 'wb') as f:
    f.write(struct.pack('<III', 0x46546C67, 2, total))
    f.write(struct.pack('<II', len(js), 0x4E4F534A)); f.write(js)
    f.write(struct.pack('<II', len(out), 0x004E4942)); f.write(out)
print(f'{src} {len(data)/1e6:.1f}MB -> {dst} {total/1e6:.1f}MB')
