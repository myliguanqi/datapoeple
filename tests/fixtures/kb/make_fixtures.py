"""生成知识库测试夹具 policy.pdf（不依赖 reportlab：手写最小 PDF）。

    python tests/fixtures/kb/make_fixtures.py

字体用 PDF 标准 CJK 字体 STSong-Light（不嵌入字形，阅读器自带或替换），编码 UniGB-UCS2-H（字符码 = UCS-2），
另附 ToUnicode CMap，pypdf 能准确提取文字。内容全部自造（店铺通用的售后与发货政策），两页，用来测页码。
"""
from __future__ import annotations

import os

HERE = os.path.dirname(os.path.abspath(__file__))

PAGES = [
    ["好物小店 售后与发货政策",
     "一、发货时间",
     "现货商品在付款后 48 小时内发货，预售商品以商品详情页标注的时间为准。",
     "默认使用中通或圆通快递，暂不支持指定快递。",
     "二、运费说明",
     "全国包邮，新疆、西藏、青海等偏远地区需补运费 10 元。"],
    ["三、退换货",
     "支持七天无理由退货，商品需保持完好、不影响二次销售，退货运费由买家承担。",
     "收到商品有质量问题的，可在签收后 15 天内申请换货，往返运费由商家承担。",
     "定制类商品和已拆封的贴身衣物不支持无理由退货。",
     "四、联系客服",
     "如有其他问题，请在商品页点击「客服」咨询，客服在线时间为每天 9:00 至 22:00。"],
]

TOUNICODE = (b"/CIDInit /ProcSet findresource begin\n12 dict begin\nbegincmap\n"
             b"/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def\n"
             b"/CMapName /Adobe-Identity-UCS def\n/CMapType 2 def\n"
             b"1 begincodespacerange\n<0000> <FFFF>\nendcodespacerange\n"
             b"1 beginbfrange\n<0000> <FFFF> <0000>\nendbfrange\n"
             b"endcmap\nCMapName currentdict /CMap defineresource pop\nend\nend\n")


def _text_ops(lines: list[str]) -> bytes:
    out = [b"BT", b"/F1 14 Tf", b"18 TL", b"50 790 Td"]
    for i, line in enumerate(lines):
        size = 18 if i == 0 else 12
        out.append(f"/F1 {size} Tf".encode())
        out.append(b"<" + line.encode("utf-16-be").hex().upper().encode() + b"> Tj T*")
        if i == 0:
            out.append(b"T*")
    out.append(b"ET")
    return b"\n".join(out)


def build_pdf(pages: list[list[str]]) -> bytes:
    objs: list[bytes] = []

    def add(body: bytes) -> int:
        objs.append(body)
        return len(objs)

    catalog = add(b"")            # 1，占位
    pages_id = add(b"")           # 2，占位
    tounicode = add(b"<< /Length %d >>\nstream\n" % len(TOUNICODE) + TOUNICODE + b"\nendstream")
    descriptor = add(b"<< /Type /FontDescriptor /FontName /STSong-Light /Flags 6 /FontBBox [-25 -254 1000 880] "
                     b"/ItalicAngle 0 /Ascent 880 /Descent -120 /CapHeight 880 /StemV 93 >>")
    cid = add(b"<< /Type /Font /Subtype /CIDFontType0 /BaseFont /STSong-Light "
              b"/CIDSystemInfo << /Registry (Adobe) /Ordering (GB1) /Supplement 4 >> "
              b"/FontDescriptor %d 0 R /DW 1000 >>" % descriptor)
    font = add(b"<< /Type /Font /Subtype /Type0 /BaseFont /STSong-Light-UniGB-UCS2-H /Encoding /UniGB-UCS2-H "
               b"/DescendantFonts [%d 0 R] /ToUnicode %d 0 R >>" % (cid, tounicode))
    kids = []
    for lines in pages:
        content = _text_ops(lines)
        c = add(b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream")
        p = add(b"<< /Type /Page /Parent %d 0 R /MediaBox [0 0 595 842] /Resources << /Font << /F1 %d 0 R >> >> "
                b"/Contents %d 0 R >>" % (pages_id, font, c))
        kids.append(p)
    objs[catalog - 1] = b"<< /Type /Catalog /Pages %d 0 R >>" % pages_id
    objs[pages_id - 1] = (b"<< /Type /Pages /Kids [" + b" ".join(b"%d 0 R" % k for k in kids) +
                          b"] /Count %d >>" % len(kids))
    info = add("<< /Title <FEFF{}> /Producer (FlashHead test fixture) >>".format(
        "好物小店售后与发货政策".encode("utf-16-be").hex().upper()).encode())

    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for i, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    for off in offsets:
        out += b"%010d 00000 n \n" % off
    out += b"trailer\n<< /Size %d /Root %d 0 R /Info %d 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objs) + 1, catalog, info, xref)
    return bytes(out)


def main() -> None:
    path = os.path.join(HERE, "policy.pdf")
    data = build_pdf(PAGES)
    with open(path, "wb") as f:
        f.write(data)
    print(f"{path}  {len(data)} 字节")


if __name__ == "__main__":
    main()
