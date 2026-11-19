"""Portable stdlib codec for passive orthogonal grid SVG and 8-bit RGBA PNG.

Rasterization samples logical cell centers; supported artwork has only filled,
axis-aligned integer rects and orthogonal polygons, without strokes/transforms.
PNG reader supports 8-bit RGB/RGBA, noninterlaced, all five PNG row filters.
"""
import re
import struct
import zlib
import xml.etree.ElementTree as ET
from pathlib import Path


def parse_svg(path, allow_diagonal=False):
    raw = Path(path).read_text()
    assert '<!' not in raw and '<?' not in raw, 'No declarations/entities'
    root = ET.fromstring(raw)
    assert root.tag == '{http://www.w3.org/2000/svg}svg'
    assert root.attrib == {'width':'256','height':'256','viewBox':'0 0 32 32','shape-rendering':'crispEdges'}, root.attrib
    shapes = []
    for node in root:
        assert len(node) == 0 and not (node.text or '').strip()
        tag = node.tag.split('}')[-1]
        a = dict(node.attrib)
        c = a.pop('fill')
        assert re.fullmatch(r'#[0-9a-fA-F]{6}', c)
        rgba = tuple(int(c[i:i+2],16) for i in (1,3,5)) + (255,)
        if tag == 'rect':
            assert set(a) == {'x','y','width','height'}
            assert all(re.fullmatch(r'\d+',v) for v in a.values())
            x,y,w,h = (int(a[k]) for k in ('x','y','width','height'))
            assert w > 0 and h > 0 and x >= 2 and y >= 2 and x+w <= 30 and y+h <= 30
            pts = [(x,y),(x+w,y),(x+w,y+h),(x,y+h)]
        else:
            assert tag == 'polygon' and set(a) == {'points'}
            tokens = a['points'].split()
            assert all(re.fullmatch(r'\d+,\d+',v) for v in tokens)
            pts = [tuple(map(int,v.split(','))) for v in tokens]
            assert len(pts) >= 4
            assert all(2 <= x <= 30 and 2 <= y <= 30 for x,y in pts), pts
            assert allow_diagonal or all(x == X or y == Y for (x,y),(X,Y) in zip(pts,pts[1:]+pts[:1])), ('diagonal',pts)
        shapes.append((pts,rgba))
    assert shapes
    return shapes


def raster(path, allow_diagonal=False):
    pixels = bytearray(32*32*4)
    for pts,color in parse_svg(path, allow_diagonal):
        for y in range(32):
            for x in range(32):
                inside = False
                for (a,b),(c,d) in zip(pts,pts[1:]+pts[:1]):
                    if (b > y+0.5) != (d > y+0.5):
                        at = (c-a)*(y+0.5-b)/(d-b)+a
                        if x+0.5 < at:
                            inside = not inside
                if inside:
                    i = (y*32+x)*4
                    pixels[i:i+4] = bytes(color)
    return bytes(pixels)


def scale(pixels, size):
    assert size in (32,64,128,256)
    factor = size//32
    return b''.join(b''.join(pixels[(y*32+x)*4:(y*32+x+1)*4]*factor for x in range(32))*factor for y in range(32))


def write_png(path, width, height, rgba):
    assert len(rgba) == width*height*4
    def chunk(kind,data):
        return struct.pack('>I',len(data))+kind+data+struct.pack('>I',zlib.crc32(kind+data)&0xffffffff)
    raw = b''.join(b'\0'+rgba[y*width*4:(y+1)*width*4] for y in range(height))
    Path(path).write_bytes(b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',struct.pack('>IIBBBBB',width,height,8,6,0,0,0))+chunk(b'IDAT',zlib.compress(raw,9))+chunk(b'IEND',b''))


def read_png(path):
    data = Path(path).read_bytes()
    assert data[:8] == b'\x89PNG\r\n\x1a\n'
    at = 8; packed = bytearray(); header = None; ended = False
    while at < len(data):
        length = struct.unpack('>I',data[at:at+4])[0]
        kind = data[at+4:at+8]
        body = data[at+8:at+8+length]
        crc = struct.unpack('>I',data[at+8+length:at+12+length])[0]
        assert crc == zlib.crc32(kind+body)&0xffffffff
        if kind == b'IHDR':
            assert header is None
            header = struct.unpack('>IIBBBBB',body)
        elif kind == b'IDAT': packed.extend(body)
        elif kind == b'IEND':
            assert length == 0
            ended = True
        at += 12+length
        if ended: break
    assert ended and at == len(data)
    w,h,depth,ctype,compress,filter_method,interlace = header
    assert depth == 8 and ctype in (2,6) and compress == filter_method == interlace == 0
    bpp = 4 if ctype == 6 else 3
    stride = w*bpp
    raw = zlib.decompress(packed)
    assert len(raw) == (stride+1)*h
    pixels = bytearray(); prior = bytearray(stride)
    for y in range(h):
        k = y*(stride+1); f = raw[k]; row = bytearray(raw[k+1:k+1+stride])
        assert f in range(5)
        for x in range(stride):
            a = row[x-bpp] if x >= bpp else 0
            b = prior[x]; c = prior[x-bpp] if x >= bpp else 0
            if f == 1: add = a
            elif f == 2: add = b
            elif f == 3: add = (a+b)//2
            elif f == 4:
                p = a+b-c; pa,pb,pc = abs(p-a),abs(p-b),abs(p-c)
                add = a if pa <= pb and pa <= pc else (b if pb <= pc else c)
            else: add = 0
            row[x] = (row[x]+add)&255
        if bpp == 4: pixels.extend(row)
        else:
            for x in range(0,stride,3): pixels.extend(row[x:x+3]+b'\xff')
        prior = row
    return w,h,bytes(pixels)
