"""Validate this immutable candidate pack using only Python's standard library.

Usage: python tools/validate.py [--v1 ../v1] [--zip /path/to/delivery.zip]
No network, application integration, semantic matching, or aesthetic approval.
Do not use python -O: the codec intentionally uses assertions for strict parsing.
"""
import argparse
import collections
import hashlib
import json
import re
import sys
import zipfile
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath

sys.dont_write_bytecode = True
from pixel_codec import parse_svg, raster, read_png, scale


def require(test, message):
    if not test:
        raise ValueError(message)


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def safe(root, relative):
    require(isinstance(relative,str) and '\\' not in relative, 'Invalid path type/separator')
    p = PurePosixPath(relative)
    require(not p.is_absolute() and p.parts and not any(v in ('.','..') for v in p.parts), 'Unsafe relative path')
    require(relative == p.as_posix() and ':' not in relative and not relative.startswith('/'), 'Unsafe/normalized path')
    target = root.joinpath(*p.parts)
    require(target.is_file() and not target.is_symlink(), 'Missing file or symlink: '+relative)
    require(root.resolve() in target.resolve().parents, 'Escaping path')
    return target


class References(HTMLParser):
    def __init__(self):
        super().__init__(); self.paths = []; self.ids = []
    def handle_starttag(self,tag,attrs):
        a = dict(attrs)
        require(tag not in ('script','iframe','object','embed','base'), 'Active HTML not allowed')
        for key in ('src','href'):
            if key in a: self.paths.append(a[key])
        require(not any(k.lower().startswith('on') for k in a), 'Active HTML attribute')


def validate(root, v1=None, archive=None):
    require(__debug__, 'Run without -O')
    catalog = json.loads((root/'catalog.json').read_text())
    items = catalog['icons']
    require(len(items)==96,'Expected 96 curated identities')
    ids = [i['id'] for i in items]
    require(len(set(ids))==len(ids),'Duplicate IDs')
    require(sorted(i['number'] for i in items)==list(range(1,97)),'Numbered inventory')
    require(catalog['status']=='candidate-not-integrated','Candidate status')
    require(catalog['logical_grid']==[32,32] and catalog['export_sizes']==[32,64,128,256],'Grid/export sizes')
    baseline = json.loads((root/'original-eight.json').read_text())
    original_ids = {'ingredient.'+s for s in ['banana','apple','avocado','egg','chicken-breast','salmon','rice','broccoli']}
    require(set(baseline)==original_ids,'Original identity list')
    require({i['id'] for i in items if i['retained_original']}==original_ids,'Retained originals')
    require({'fallback.food','fallback.meal'} <= set(ids),'Explicit neutral fallbacks')
    aliases = []; svghashes = []; decodedhashes = []; refs = []; bbox = {}; legacy_delta = None
    for i in items:
        require(re.fullmatch(r'(ingredient|prepared|drink|fallback)\.[a-z][a-z0-9-]*',i['id']), 'ID syntax')
        require(i['id'].startswith(i['kind']+'.'), 'Identity namespace')
        require(i['default_fallback'] in ('fallback.food','fallback.meal'), 'Fallback reference')
        require(bool(i['fallback_intent']) == (i['kind']=='fallback'), 'Fallback intent')
        for alias in i['proposed_exact_aliases']:
            require(alias == alias.strip().lower() and bool(re.fullmatch(r'[a-z]+(?: [a-z]+)*',alias)), 'Conservative exact alias')
            aliases.append(alias)
        svg = safe(root,i['svg']); refs.append(i['svg']);svghashes.append(sha(svg))
        allow_legacy = i['id']=='ingredient.broccoli'
        pixels = raster(svg,allow_diagonal=allow_legacy)
        coords = [(k%32,k//32) for k,a in enumerate(pixels[3::4]) if a]
        require(coords,'Empty silhouette')
        x0,y0 = min(x for x,y in coords),min(y for x,y in coords)
        x1,y1 = max(x for x,y in coords),max(y for x,y in coords)
        require(min(x0,y0)>=2 and max(x1,y1)<=29,'Logical transparent margins')
        bbox[i['id']] = [x0,y0,x1+1,y1+1]
        decodedhashes.append(hashlib.sha256(pixels).hexdigest())
        require(set(i['png'])=={'32','64','128','256'},'Required sizes')
        for size in (32,64,128,256):
            rel = i['png'][str(size)]; refs.append(rel); path = safe(root,rel)
            w,h,actual = read_png(path)
            require((w,h)==(size,size),'PNG dimensions')
            alpha = set(actual[3::4]);require(alpha=={0,255},'Decoded binary alpha/transparency')
            margin = 2*(size//32)
            for y in range(size):
                require(not any(actual[(y*size+x)*4+3] for x in range(margin)), 'Left alpha margin')
                require(not any(actual[(y*size+x)*4+3] for x in range(size-margin,size)), 'Right alpha margin')
            require(not any(actual[3:margin*size*4:4]),'Top alpha margin')
            require(not any(actual[(size-margin)*size*4+3::4]),'Bottom alpha margin')
            expected = scale(pixels,size)
            if allow_legacy and size==256:
                legacy_delta = sum(actual[k:k+4] != expected[k:k+4] for k in range(0,len(actual),4))
            else:
                require(actual==expected,'SVG/nearest-neighbor PNG equivalence: '+rel)
        if i['retained_original']:
            b=baseline[i['id']]
            require(sha(svg)==b['svg_sha256'] and sha(root/i['png']['256'])==b['png256_sha256'], 'Frozen original hashes')
            if v1:
                require(svg.read_bytes()==safe(v1,b['v1_svg']).read_bytes(),'Original SVG byte identity')
                require((root/i['png']['256']).read_bytes()==safe(v1,b['v1_png256']).read_bytes(),'Original 256 PNG byte identity')
    require(len(set(aliases))==len(aliases),'Duplicate exact aliases')
    require(len(set(refs))==len(refs),'Reused asset paths')
    require(len(set(svghashes))==96,'Exact SVG duplicate')
    require(len(set(decodedhashes))==96,'Exact decoded silhouette duplicate')
    actual_svgs={p.relative_to(root).as_posix() for p in (root/'svg').rglob('*') if p.is_file()}
    actual_pngs={p.relative_to(root).as_posix() for p in (root/'png').rglob('*') if p.is_file()}
    require(actual_svgs=={i['svg'] for i in items},'Exact SVG folder inventory')
    require(actual_pngs=={v for i in items for v in i['png'].values()},'Exact PNG export inventory')
    sheets = json.loads((root/'sheets/index.json').read_text())
    require(len(sheets)==16,'Category sheet count')
    sheet_ids = [s for sh in sheets for s in sh['ids']]
    require(sorted(sheet_ids)==sorted(ids),'Every icon appears once on category sheets')
    for sh in sheets:
        w,h,_=read_png(safe(root,sh['png']))
        require((w,h)==(sh['width'],sh['height']),'Sheet dimensions')
        safe(root,sh['html'])
    htmlfiles = sorted(root.rglob('*.html'))
    for path in htmlfiles:
        text=path.read_text(); parser=References();parser.feed(text)
        for ref in parser.paths + re.findall(r'url\(([^)]+)\)',text):
            require(not any(v in ref for v in (':','?','#','\\')),'Nonlocal HTML reference')
            target=(path.parent/ref).resolve()
            require(root.resolve() in target.parents and target.is_file(),'Missing/unsafe preview reference')
    hashes=json.loads((root/'SHA256.json').read_text())
    files={p.relative_to(root).as_posix():p for p in root.rglob('*') if p.is_file()}
    require(set(hashes)==set(files)-{'SHA256.json'},'Full file inventory (no extras)')
    for rel,digest in hashes.items():
        require(sha(safe(root,rel))==digest,'File hash: '+rel)
    for rel,path in files.items():
        require(not path.is_symlink(),'No symlinks')
        require(not any(v in rel for v in ('__pycache__','.DS_Store','.env','STATUS.md','RECEIPT.json','RESULT.json')),'Mutable/unrelated file')
        if path.suffix in ('.md','.json','.py','.html','.svg','.txt'):
            text=path.read_text()
            markers = (chr(47)+'Users'+chr(47), 'C:'+chr(92)+'Users'+chr(92), 'gh'+'p_', 'gh'+'o_', 'BEGIN '+'PRIVATE KEY', 'BEGIN '+'OPENSSH PRIVATE KEY')
            require(not any(v in text for v in markers), 'Machine path/credential marker')
    archive_members=None
    if archive:
        with zipfile.ZipFile(archive) as z:
            require(z.testzip() is None,'ZIP CRC integrity')
            names=z.namelist();require(len(names)==len(set(names)),'Duplicate ZIP members')
            require(set(names)==set(files),'ZIP/source file inventory')
            for rel in names:
                safe(root,rel)
                require(z.read(rel)==files[rel].read_bytes(),'ZIP/source bytes: '+rel)
            archive_members=len(names)
    return {'status':'automated-verified-candidate','icons':len(items),'retained_originals':len(original_ids),'new_icons':len(items)-len(original_ids),'svg_files':len(svghashes),'transparent_png_exports':len(items)*4,'category_counts':dict(collections.Counter(i['category'] for i in items)),'category_sheets':len(sheets),'html_files':len(htmlfiles),'hashed_files':len(hashes),'zip_members':archive_members,'originals_compared_to_v1':bool(v1),'inherited_broccoli_256_vs_grid_differing_pixels':legacy_delta,'limitations':['No independent review, native integration, device tests or semantic recognition is established by this validator.','Inherited broccoli SVG has a diagonal stem edge; its unchanged 256 PNG is exempt from new-grid equivalence, but not byte/hash/alpha validation.','Checksums establish internal integrity, not authorship or external trust. PNG codec is restricted to noninterlaced 8-bit RGB/RGBA.']}


def main():
    a=argparse.ArgumentParser(description=__doc__);a.add_argument('--v1',type=Path);a.add_argument('--zip',type=Path);a.add_argument('--root',type=Path,default=Path(__file__).resolve().parents[1]);args=a.parse_args()
    print(json.dumps(validate(args.root,args.v1,args.zip),indent=2))

if __name__=='__main__':main()
