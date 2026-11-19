"""Compare a converted .docx against the original .docx it was printed from.

Usage: python compare.py original.docx converted.docx [--render]
Reports: size histogram (by characters), font names, paragraph/table counts,
text similarity, and (with --render) page count via LibreOffice.
"""
import collections, difflib, os, re, subprocess, sys, tempfile
import docx
from docx.oxml.ns import qn


def eff_size(run, para, doc):
    if run.font.size:
        return run.font.size.pt
    st = para.style
    while st is not None:
        if st.font.size:
            return st.font.size.pt
        st = st.base_style
    return 11.0


def fonts_of(run):
    rpr = run._r.rPr
    if rpr is None or rpr.rFonts is None:
        return None
    rf = rpr.rFonts
    return rf.get(qn("w:eastAsia")) or rf.get(qn("w:ascii"))


def body_paras(d):
    from docx.table import Table
    from docx.text.paragraph import Paragraph
    for el in d.element.body.iterchildren():
        if el.tag == qn("w:p"):
            yield Paragraph(el, d)
        elif el.tag == qn("w:tbl"):
            seen = []  # kept alive on purpose: id() of a freed proxy can be reused
            for row in Table(el, d).rows:
                for c in row.cells:
                    if any(c._tc is s for s in seen):
                        continue  # a merged cell is returned once per grid slot
                    seen.append(c._tc)
                    yield from c.paragraphs


def stats(path):
    d = docx.Document(path)
    sizes, fonts = collections.Counter(), collections.Counter()
    text = []
    for p in body_paras(d):
        for r in p.runs:
            n = len(r.text.strip())
            if not n:
                continue
            sizes[round(eff_size(r, p, d) * 2) / 2] += n
            fonts[fonts_of(r) or "(style)"] += n
        text.append(p.text)
    hdr = sum(len(p.text.strip()) for s in d.sections for p in s.header.paragraphs + s.footer.paragraphs)
    return dict(sizes=sizes, fonts=fonts, text=re.sub(r"\s+", "", "".join(text)),
                paras=sum(1 for p in d.paragraphs if p.text.strip()), tables=len(d.tables),
                table_rows=[len(t.rows) for t in d.tables], sections=len(d.sections), header_chars=hdr)


def pages(path):
    out = tempfile.mkdtemp()
    subprocess.run(["soffice", "--headless", "--convert-to", "pdf", "--outdir", out, path],
                   capture_output=True, timeout=180)
    pdf = os.path.join(out, os.path.splitext(os.path.basename(path))[0] + ".pdf")
    info = subprocess.run(["pdfinfo", pdf], capture_output=True, text=True).stdout
    m = re.search(r"Pages:\s+(\d+)", info)
    return int(m.group(1)) if m else None


def main():
    a, b = sys.argv[1], sys.argv[2]
    sa, sb = stats(a), stats(b)
    def top(c, k=8):
        tot = sum(c.values()) or 1
        return ", ".join(f"{k_}:{v*100//tot}%" for k_, v in c.most_common(k))
    print("sizes  orig:", top(sa["sizes"]))
    print("sizes  conv:", top(sb["sizes"]))
    print("fonts  conv:", top(sb["fonts"], 6))
    print(f"paras {sa['paras']} -> {sb['paras']} | tables {sa['table_rows']} -> {sb['table_rows']} | sections {sa['sections']} -> {sb['sections']} | header/footer chars {sa['header_chars']} -> {sb['header_chars']}")
    # body text of the original excludes its header/footer; compare body text only
    sm = difflib.SequenceMatcher(None, sa["text"], sb["text"], autojunk=False)
    print(f"text similarity {sm.ratio():.3f}")
    if "--diff" in sys.argv:
        for op, i1, i2, j1, j2 in sm.get_opcodes():
            if op != "equal":
                print(f"  {op}: {sa['text'][i1:i2][:40]!r} -> {sb['text'][j1:j2][:40]!r}")
    if "--render" in sys.argv:
        print("pages  orig:", pages(a), " conv:", pages(b))


main()
