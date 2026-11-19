"""Portable negative/codec tests: python tools/test_validator.py"""
import json
import struct
import sys
import tempfile
import unittest
import zlib
from pathlib import Path
sys.dont_write_bytecode = True
from pixel_codec import parse_svg, raster, read_png, write_png
from validate import safe, validate


class ValidationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
    def tearDown(self):
        self.temp.cleanup()
    def test_png_roundtrip_and_crc_rejection(self):
        p = self.root/'test.png'
        raw = bytes([0,0,0,0,100,110,120,255])*2
        write_png(p,2,2,raw)
        self.assertEqual(read_png(p),(2,2,raw))
        damaged = bytearray(p.read_bytes()); damaged[29] ^= 1;p.write_bytes(damaged)
        with self.assertRaises(AssertionError):read_png(p)
    def test_all_png_row_filters(self):
        w,h=3,2
        raw=bytes((i*17)%256 for i in range(w*h*4))
        def chunk(k,b):return struct.pack('>I',len(b))+k+b+struct.pack('>I',zlib.crc32(k+b)&0xffffffff)
        for filt in range(5):
            data=bytearray(); prior=bytes(w*4)
            for y in range(h):
                row=raw[y*w*4:(y+1)*w*4]; data.append(filt)
                for x,value in enumerate(row):
                    a=row[x-4] if x>=4 else 0;b=prior[x];c=prior[x-4] if x>=4 else 0
                    if filt==0:add=0
                    elif filt==1:add=a
                    elif filt==2:add=b
                    elif filt==3:add=(a+b)//2
                    else:
                        p=a+b-c;pa,pb,pc=abs(p-a),abs(p-b),abs(p-c)
                        add=a if pa<=pb and pa<=pc else (b if pb<=pc else c)
                    data.append((value-add)&255)
                prior=row
            p=self.root/f'filter{filt}.png'
            p.write_bytes(b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',struct.pack('>IIBBBBB',w,h,8,6,0,0,0))+chunk(b'IDAT',zlib.compress(data))+chunk(b'IEND',b''))
            self.assertEqual(read_png(p),(w,h,raw))
    def test_safe_relative_paths(self):
        (self.root/'okay').write_text('x')
        self.assertEqual(safe(self.root,'okay'),self.root/'okay')
        for bad in ('../okay','/okay','https://example.org','a\\b','a/../okay','./okay'):
            with self.assertRaises(ValueError):safe(self.root,bad)
    def test_duplicate_catalog_ids_rejected(self):
        icons=[{'id':'ingredient.same','number':n} for n in range(1,97)]
        (self.root/'catalog.json').write_text(json.dumps({'icons':icons}))
        with self.assertRaisesRegex(ValueError,'Duplicate IDs'):validate(self.root)
    def test_passive_svg_and_integer_grid(self):
        start='<svg xmlns="http://www.w3.org/2000/svg" width="256" height="256" viewBox="0 0 32 32" shape-rendering="crispEdges">'
        p=self.root/'test.svg'
        p.write_text(start+'<rect x="3" y="3" width="5" height="5" fill="#4a5036"/></svg>')
        self.assertEqual(sum(bool(a) for a in raster(p)[3::4]),25)
        for content in ('<script/>','<circle cx="10" cy="10" r="4" fill="#4a5036"/>','<rect x="3.5" y="3" width="5" height="5" fill="#4a5036"/>','<polygon points="3,3 10,3 10,10 4,10" fill="#4a5036"/>'):
            p.write_text(start+content+'</svg>')
            with self.assertRaises((AssertionError,KeyError)):parse_svg(p)


if __name__=='__main__':unittest.main()
