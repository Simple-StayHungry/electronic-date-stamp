import os
import sys
from pathlib import Path
import fitz
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from core.fonts import FontResolver

@pytest.fixture(scope='session')
def fonts():
    f=FontResolver()
    sample=os.environ.get('LUOKUAN_TEST_PDF')
    if f.times is None and sample:
        with fitz.open(sample) as d:f.use_document(d)
    if f.times is None:pytest.skip('需要本机 Times New Roman Regular 或 LUOKUAN_TEST_PDF 指定的内嵌字体样本。')
    return f
