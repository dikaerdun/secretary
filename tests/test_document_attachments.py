import asyncio
from io import BytesIO
import zipfile

import pytest

from secretary.customer_store import CustomerStore
from secretary.materials import MaterialService
from secretary.document_attachments import DocumentAttachmentService, extract_document


def office(parts):
    out = BytesIO()
    with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED) as archive:
        for name, content in parts.items():
            archive.writestr(name, content)
    return out.getvalue()


def test_docx_paragraph_and_table_keep_source_text():
    raw = office({'word/document.xml': '<w:document xmlns:w="urn:w"><w:body><w:p><w:r><w:t>试点方案</w:t></w:r></w:p><w:tbl><w:tr><w:tc><w:p><w:r><w:t>预算尚未审批</w:t></w:r></w:p></w:tc></w:tr></w:tbl></w:body></w:document>'})
    value = extract_document('方案.docx', raw)
    assert value['parse_status'] == 'ready'
    assert '试点方案' in value['text'] and '预算尚未审批' in value['text']


def test_slides_sort_numerically_and_keep_notes():
    raw = office({f'ppt/slides/slide{i}.xml': f'<a xmlns="urn:a"><t>第{i}页</t></a>' for i in (10, 2, 1)} | {'ppt/notesSlides/notesSlide9.xml': '<a xmlns="urn:a"><t>先核实范围</t></a>', 'ppt/slides/_rels/slide1.xml.rels':'<Relationships><Relationship Id="r1" Type="urn:office/notesSlide" Target="../notesSlides/notesSlide9.xml"/></Relationships>'})
    text = extract_document('汇报.pptx', raw)['text']
    assert text.index('第1页') < text.index('第2页') < text.index('第10页')
    assert '先核实范围' in text


def test_reordered_office_parts_follow_relationships_not_numbers():
    sheet = lambda text: f'<worksheet><row><c t="inlineStr"><is><t>{text}</t></is></c></row></worksheet>'
    raw = office({'xl/workbook.xml':'<workbook xmlns:r="urn:r"><sheets><sheet name="项目甲" r:id="r2"/><sheet name="项目乙" r:id="r1"/></sheets></workbook>',
        'xl/_rels/workbook.xml.rels':'<Relationships><Relationship Id="r1" Type="urn:office/worksheet" Target="worksheets/sheet1.xml"/><Relationship Id="r2" Type="urn:office/worksheet" Target="/xl/worksheets/sheet2.xml"/></Relationships>',
        'xl/worksheets/sheet1.xml':sheet('乙预算50万'),'xl/worksheets/sheet2.xml':sheet('甲预算30万')})
    text = extract_document('项目预算.xlsx',raw)['text']
    assert text.index('项目甲') < text.index('甲预算30万') < text.index('项目乙') < text.index('乙预算50万')
    raw=office({'ppt/presentation.xml':'<p xmlns:r="urn:r"><sldId r:id="r2"/><sldId r:id="r1"/></p>',
        'ppt/_rels/presentation.xml.rels':'<Relationships><Relationship Id="r1" Type="urn:office/slide" Target="slides/slide1.xml"/><Relationship Id="r2" Type="urn:office/slide" Target="slides/slide2.xml"/></Relationships>',
        'ppt/slides/slide1.xml':'<a><t>实际第二页</t></a>','ppt/slides/slide2.xml':'<a><t>实际第一页</t></a>',
        'ppt/notesSlides/notesSlide1.xml':'<a><t>无关备注不可猜配</t></a>'})
    text=extract_document('重排汇报.pptx',raw)['text']
    assert text.index('实际第一页')<text.index('实际第二页') and '无关备注不可猜配' not in text


def test_xlsx_shared_strings_and_cached_values():
    raw = office({'xl/sharedStrings.xml': '<sst><si><t>项目范围</t></si></sst>', 'xl/worksheets/sheet1.xml': '<worksheet><sheetData><row><c t="s"><v>0</v></c><c><v>250000</v></c></row></sheetData></worksheet>'})
    text = extract_document('预算.xlsx', raw)['text']
    assert '项目范围' in text and '250000' in text


def test_unreadable_pdf_keeps_original_and_explicit_needs_text():
    from pypdf import PdfWriter
    pdf = PdfWriter(); pdf.add_blank_page(width=300, height=300)
    buffer = BytesIO(); pdf.write(buffer)
    value = extract_document('扫描件.pdf', buffer.getvalue())
    assert value['parse_status'] == 'needs_text' and value['text'] == ''
    assert value['parse_error']


def test_image_only_office_is_unreadable_and_partial_is_disclosed():
    for name, parts in [('图片汇报.pptx', {'ppt/slides/slide1.xml':'<a><pic/></a>'}),
                        ('空表.xlsx', {'xl/worksheets/sheet1.xml':'<worksheet><row><c r="A1"/></row></worksheet>'})]:
        value=extract_document(name,office(parts))
        assert value['parse_status']=='needs_text' and not value['text']
    value=extract_document('部分汇报.pptx',office({'ppt/slides/slide1.xml':'<a><pic/></a>','ppt/slides/slide2.xml':'<a><t>范围尚待确认</t></a>'}))
    assert value['parse_status']=='ready' and '范围尚待确认' in value['text']
    assert '部分幻灯片' in value['parse_error'] and '图片' in value['parse_error']


def test_untrusted_formats_and_xml_entities_rejected_or_unreadable():
    with pytest.raises(ValueError):
        extract_document('run.exe', b'content')
    raw = office({'word/document.xml': '<!DOCTYPE a [<!ENTITY x "fake">]><a>&x;</a>'})
    assert extract_document('方案.docx', raw)['parse_status'] == 'needs_text'


def test_upload_keeps_binary_owner_idempotency_and_manual_text_recovery(tmp_path):
    crm = CustomerStore(tmp_path / 'files.sqlite3')
    try:
        materials = MaterialService(crm, asyncio.Lock())
        files = DocumentAttachmentService(crm, materials)
        value = files.upload('alice', '报告.txt', '密码平台试点，预算尚未审批。'.encode(), 'key-1')
        mid = value['material']['id']
        assert files.upload('alice', '报告.txt', '密码平台试点，预算尚未审批。'.encode(), 'key-1')['material']['id'] == mid
        with pytest.raises(ValueError):
            files.upload('alice', '报告.txt', b'changed', 'key-1')
        with pytest.raises(KeyError):
            files.get('bob', mid)
        saved = files.get('alice', mid)
        assert saved['content'] == '密码平台试点，预算尚未审批。'.encode()
        assert materials.detail('alice', mid)['text'] == '密码平台试点，预算尚未审批。'
        assert crm._db.execute('SELECT count(*) FROM tasks').fetchone()[0] == 0
    finally:
        crm.close()


def test_document_profile_source_is_reference_not_customer_commitment(tmp_path):
    from secretary.sales_workspace import SalesWorkspace
    from secretary.profile_intelligence import ProfileIntelligence, _source_basis
    crm=CustomerStore(tmp_path/'profile.sqlite3')
    try:
        materials=MaterialService(crm,asyncio.Lock())
        files=DocumentAttachmentService(crm,materials)
        unit=crm.create_customer('alice',{'name':'虚构演练单位'},1800000000)
        value=files.upload('alice','项目汇报.txt','客户表示需求已确认。'.encode(),'profile-doc')
        material=materials.update('alice',value['material']['id'],{'revision':1,'customer_id':unit['id']})
        profile=ProfileIntelligence(crm,SalesWorkspace(crm),lock=asyncio.Lock())
        source=profile._sources(crm._db,'alice',unit['id'])[0]
        assert source['source_nature']=='document' and source['filename']=='项目汇报.txt'
        assert _source_basis(source,'客户表示需求已确认。','reported',{'contacts':[]})=='observation'
        assert crm._db.execute('SELECT count(*) FROM crm_customer_facts').fetchone()[0]==0
        unread=files.upload('alice','照片.png',b'\x89PNG\r\n\x1a\ncontent','unread')
        materials.update('alice',unread['material']['id'],{'revision':1,'customer_id':unit['id']})
        assert len(profile._sources(crm._db,'alice',unit['id']))==1
        with pytest.raises(ValueError):
            materials.retry('alice',unread['material']['id'],2)
    finally:crm.close()
