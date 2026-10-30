#!/usr/bin/env python3
"""Compare the installed official client's nfapi imports with this shim."""
import argparse
from pathlib import Path
import struct

class PE:
    def __init__(self, path):
        self.data = Path(path).read_bytes()
        d = self.data
        if d[:2] != b"MZ":
            raise ValueError("Not a PE image")
        nt = struct.unpack_from("<I", d, 60)[0]
        if d[nt:nt+4] != b"PE\0\0":
            raise ValueError("Invalid PE signature")
        count = struct.unpack_from("<H",d,nt+6)[0]
        optsize = struct.unpack_from("<H",d,nt+20)[0]
        opt = nt+24
        magic = struct.unpack_from("<H",d,opt)[0]
        self.width = 8 if magic == 0x20b else 4 if magic == 0x10b else 0
        if not self.width or count > 96:
            raise ValueError("Unsupported PE image")
        directory = opt+(112 if self.width==8 else 96)
        self.exports = struct.unpack_from("<II",d,directory)
        self.imports = struct.unpack_from("<II",d,directory+8)
        self.sections = []
        for i in range(count):
            p=opt+optsize+40*i
            virtual_size,va,raw_size,raw=struct.unpack_from("<IIII",d,p+8)
            self.sections.append((va,max(virtual_size,raw_size),raw,raw_size))
    def offset(self,rva,size=1):
        for va,span,raw,raw_size in self.sections:
            if va<=rva and rva-va+size<=raw_size and raw+rva-va+size<=len(self.data):
                return raw+rva-va
        raise ValueError("PE RVA outside file-backed sections")
    def string(self,rva):
        offset=self.offset(rva)
        end=self.data.find(b"\0",offset,min(offset+4096,len(self.data)))
        if end<0:raise ValueError("Unterminated PE string")
        return self.data[offset:end].decode("ascii")
    def exported_names(self):
        if not self.exports[0]:return set()
        fields=struct.unpack_from("<IIHHIIIIIII",self.data,self.offset(self.exports[0],40))
        count,names=fields[7],fields[9]
        if count>65536:raise ValueError("Absurd PE export count")
        return {self.string(struct.unpack_from("<I",self.data,self.offset(names+4*i,4))[0]) for i in range(count)}
    def imported_names(self,dll):
        result=set()
        if not self.imports[0]:return result
        for i in range(4096):
            fields=struct.unpack_from("<IIIII",self.data,self.offset(self.imports[0]+20*i,20))
            if not any(fields):return result
            thunk,_,_,name,first=fields
            if self.string(name).casefold()!=dll.casefold():continue
            for j in range(65536):
                entry=struct.unpack_from("<Q" if self.width==8 else "<I",self.data,
                    self.offset((thunk or first)+j*self.width,self.width))[0]
                if not entry:break
                if entry & (1<<(self.width*8-1)):result.add("#"+str(entry&0xffff))
                else:result.add(self.string(entry+2))
            else:raise ValueError("Absurd PE import count")
        raise ValueError("Unterminated PE import directory")

def check(shim,app):
    provided=PE(shim).exported_names()
    required=set()
    checked=0
    for path in Path(app).iterdir():
        if path.suffix.lower() not in (".dll",".exe"):continue
        try:imports=PE(path).imported_names("nfapi.dll")
        except (ValueError,struct.error,UnicodeDecodeError):continue
        if imports:checked+=1;required.update(imports)
    if not required:
        raise ValueError("No nfapi imports found; cannot validate this client")
    missing=required-provided
    print(f"nfapi ABI: {checked} client modules, {len(required)} required, {len(provided)} provided")
    if missing:
        print("Missing exports:\n"+"\n".join(sorted(missing)))
        return False
    print("All required nfapi exports are present.")
    return True

if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("shim",type=Path)
    parser.add_argument("app",type=Path)
    args=parser.parse_args()
    try:ok=check(args.shim,args.app)
    except (OSError,ValueError,struct.error) as error:parser.exit(1,str(error)+"\n")
    raise SystemExit(0 if ok else 1)
