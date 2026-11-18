# -*- coding: utf-8 -*-
"""解析 MP4 stts/stsz 采样表，找出视频轨时间戳异常（确定性，不依赖解码）"""
import struct, sys, os

def find_atoms(data, start, end, target):
    """迭代找指定层级的 atom，返回 {name: (offset_of_body, size)}"""
    out = {}
    pos = start
    while pos + 8 <= end:
        size = struct.unpack(">I", data[pos:pos+4])[0]
        name = data[pos+4:pos+8].decode("latin1")
        if size == 1:
            size = struct.unpack(">Q", data[pos+8:pos+16])[0]
            hdr = 16
        else:
            hdr = 8
        if size == 0:
            size = end - pos
        if size < hdr:
            break
        if name in target and name not in out:
            out[name] = (pos + hdr, size - hdr)
        pos += size
    return out

def video_stts(path):
    data = open(path, "rb").read()
    top = find_atoms(data, 0, len(data), {"moov"})
    moov_s, moov_sz = top["moov"]
    moov_e = moov_s + moov_sz
    # 逐 trak 找 vide 轨
    pos = moov_s
    while pos + 8 <= moov_e:
        size = struct.unpack(">I", data[pos:pos+4])[0]
        name = data[pos+4:pos+8].decode("latin1")
        if name == "trak":
            trak = find_atoms(data, pos+8, pos+size, {"mdia"})
            mdia_s, mdia_sz = trak["mdia"]
            md = find_atoms(data, mdia_s, mdia_s+mdia_sz, {"hdlr", "minf", "mdhd"})
            h_s, h_sz = md["hdlr"]
            if b"vide" in data[h_s:h_s+h_sz]:
                # 视频轨时基（mdhd timescale），不要写死
                mdhd_s, mdhd_sz = md["mdhd"]
                version = data[mdhd_s]
                if version == 1:
                    ts = struct.unpack(">I", data[mdhd_s+20:mdhd_s+24])[0]
                else:
                    ts = struct.unpack(">I", data[mdhd_s+12:mdhd_s+16])[0]
                minf_s, minf_sz = md["minf"]
                mi = find_atoms(data, minf_s, minf_s+minf_sz, {"stbl"})
                stbl_s, stbl_sz = mi["stbl"]
                st = find_atoms(data, stbl_s, stbl_s+stbl_sz, {"stts", "stsz"})
                # stts
                s, _ = st["stts"]
                n = struct.unpack(">I", data[s+4:s+8])[0]
                entries = []
                for i in range(n):
                    c, d = struct.unpack(">II", data[s+8+i*8:s+16+i*8])
                    entries.append((c, d))
                # stsz 样本总数
                z, _ = st["stsz"]
                nsamp = struct.unpack(">I", data[z+8:z+12])[0]
                return entries, nsamp, ts
        pos += size
    return None, None, None

if __name__ == "__main__":
    for f in sys.argv[1:]:
        r = video_stts(f)
        entries = r[0] if r else None
        if entries is None:
            print(f, "| 未找到视频轨")
            continue
        nsamp, ts_scale = video_stts(f)[1], video_stts(f)[2]
        # 重建时间轴，找异常（delta=0 或突变）
        t = 0
        for c, d in entries:
            t += c * d
        print(os.path.basename(f), "| 样本数:", nsamp, "| 时基:", ts_scale,
              "| stts 条目:", len(entries), "| 时间跨度(s):", round(t/ts_scale, 2))
        # 异常：delta==0 的连续段
        zero = [(c, d) for c, d in entries if d == 0 and c > 0]
        print("  delta=0 条目:", zero[:5], "| 合计样本:", sum(c for c, d in zero))
        # 变小的 delta（时间回退不可能，但 delta 忽大忽小要列 top）
        dd = sorted(entries, key=lambda x: -x[1])[:5]
        print("  最大 delta 条目:", dd)
