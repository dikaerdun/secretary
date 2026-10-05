"""Original chat documents and bounded text extraction; contents never execute."""
from __future__ import annotations

from io import BytesIO
import hashlib
import posixpath
import re
import time
import zipfile
from xml.etree import ElementTree as ET

from .crm import _owner, _identifier, _text

MAX_BYTES = 20 * 1024 * 1024
MAX_TEXT = 500_000
SUPPORTED = ('.pdf', '.docx', '.pptx', '.xlsx', '.txt', '.md', '.csv', '.png', '.jpg', '.jpeg', '.doc', '.ppt', '.xls')


def filename(value):
    value = _text(value, '文件名', 240, required=True).replace('\\', '/').rsplit('/', 1)[-1]
    value = re.sub(r'[\x00-\x1f\x7f]', '', value).strip()
    if not value or '.' not in value or '.' + value.rsplit('.', 1)[-1].lower() not in SUPPORTED:
        raise ValueError('请上传 PDF、Word、PPT、Excel、文本或图片材料。')
    return value


def _xml(raw):
    if b'<!DOCTYPE' in raw.upper() or b'<!ENTITY' in raw.upper():
        raise ValueError('文件 XML 内容无法安全读取，请另存标准格式或补充文字。')
    return ET.fromstring(raw)


def _tag(node):
    return node.tag.rsplit('}', 1)[-1]


def _office(raw, suffix):
    with zipfile.ZipFile(BytesIO(raw)) as archive:
        infos = archive.infolist()
        if len(infos) > 5000 or sum(i.file_size for i in infos) > 60 * 1024 * 1024:
            raise ValueError('文件展开内容过大，请拆分或补充重点文字。')
        names = set(archive.namelist())
        def read(name):
            data = archive.read(name)
            if len(data) > 10 * 1024 * 1024:
                raise ValueError('单页内容过大，请拆分。')
            return _xml(data)
        def relationships(part):
            path = posixpath.join(posixpath.dirname(part), '_rels', posixpath.basename(part) + '.rels')
            if path not in names:
                return {}
            result = {}
            for rel in read(path):
                if rel.get('TargetMode') == 'External':
                    continue
                target = rel.get('Target', '')
                resolved = posixpath.normpath(target.lstrip('/') if target.startswith('/') else
                                             posixpath.join(posixpath.dirname(part), target))
                if resolved in names and not resolved.startswith('../'):
                    result[rel.get('Id')] = (resolved, rel.get('Type', '').rsplit('/', 1)[-1])
            return result
        def ordered_parts(part, tag, kind, fallback):
            # The visible order and labels live in the manifest, not in XML file numbers.
            if part not in names:
                return [(name, '') for name in fallback]
            relations = relationships(part)
            ordered = []
            for node in read(part).iter():
                if _tag(node) != tag:
                    continue
                rid = next((v for k, v in node.attrib.items() if k.endswith('}id')), None)
                value = relations.get(rid)
                if value and value[1] == kind:
                    ordered.append((value[0], node.get('name', '')))
            # Broken manifests must not assign an unrelated label to a guessed file.
            return ordered or [(name, '') for name in fallback]
        if suffix == '.docx':
            root = read('word/document.xml')
            return ('\n'.join(''.join(n.text or '' for n in p.iter() if _tag(n) == 't')
                             for p in root.iter() if _tag(p) == 'p'),
                    '只读取了文字，文件中的图片内容尚未识别。' if any(_tag(n) in ('drawing','pict') for n in root.iter()) else '')
        if suffix == '.pptx':
            slides = sorted((n for n in names if re.fullmatch(r'ppt/slides/slide\d+\.xml', n)),
                            key=lambda n: int(re.search(r'(\d+)\.xml$', n)[1]))
            if not slides:
                raise ValueError('没有找到演示文稿正文。')
            slides = ordered_parts('ppt/presentation.xml', 'sldId', 'slide', slides)
            parts, missing, images = [], [], False
            for index, (name, _) in enumerate(slides, 1):
                root = read(name)
                body = '\n'.join(n.text or '' for n in root.iter() if _tag(n) == 't').strip()
                images = images or any(_tag(n) == 'pic' for n in root.iter())
                if body:
                    parts.append(f'【幻灯片 {index}】\n' + body)
                else:
                    missing.append(index)
                notes = next((target for target, kind in relationships(name).values() if kind == 'notesSlide'), None)
                if notes:
                    body = '\n'.join(n.text or '' for n in read(notes).iter() if _tag(n) == 't').strip()
                    if body:
                        parts.append(f'【幻灯片 {index} 备注】\n' + body)
            warning = ('部分幻灯片没有可读文字；' if missing else '') + ('图片内容尚未识别；' if images else '')
            return '\n\n'.join(parts), warning + '请结合原件核对。' if warning else ''
        shared = []
        if 'xl/sharedStrings.xml' in names:
            shared = [''.join(n.text or '' for n in si.iter() if _tag(n) == 't')
                      for si in read('xl/sharedStrings.xml').iter() if _tag(si) == 'si']
        sheets = sorted((n for n in names if re.fullmatch(r'xl/worksheets/sheet\d+\.xml', n)),
                        key=lambda n: int(re.search(r'(\d+)\.xml$', n)[1]))
        if not sheets:
            raise ValueError('没有找到工作表。')
        sheets = ordered_parts('xl/workbook.xml', 'sheet', 'worksheet', sheets)
        parts, missing = [], False
        for index, (name, label) in enumerate(sheets, 1):
            rows, has_content = [], False
            for row in read(name).iter():
                if _tag(row) != 'row':
                    continue
                values = []
                for cell in row:
                    vals = [n.text or '' for n in cell.iter() if _tag(n) in ('v', 't')]
                    value = ''.join(vals)
                    if cell.get('t') == 's':
                        value = shared[int(value)] if value.isdigit() and int(value) < len(shared) else ''
                    has_content = has_content or bool(value.strip())
                    if any(_tag(n) == 'f' for n in cell) and not value:
                        value = '[公式结果未保存]'
                    values.append(f'{cell.get("r", "")}: {value}' if cell.get('r') else value)
                rows.append('\t'.join(values))
            if has_content:
                parts.extend([f'【工作表：{label or name}】', *rows])
            else:
                missing = True
        return '\n'.join(parts), '部分工作表没有可读数据，图片和未保存的公式结果尚未识别。' if missing else ''


def extract_document(name, raw):
    name = filename(name)
    if not isinstance(raw, bytes) or not raw or len(raw) > MAX_BYTES:
        raise ValueError('文件不能为空，且每份不能超过20MB。')
    suffix = '.' + name.rsplit('.', 1)[-1].lower()
    text, error, incomplete = '', '', False
    try:
        if suffix in ('.docx', '.pptx', '.xlsx'):
            text, error = _office(raw, suffix)
        elif suffix == '.pdf':
            if not raw.startswith(b'%PDF-'):
                raise ValueError('文件不是可读的 PDF。')
            from pypdf import PdfReader
            reader = PdfReader(BytesIO(raw))
            if reader.is_encrypted:
                raise ValueError('PDF已加密，请提供未加密版本或粘贴重点文字。')
            if len(reader.pages) > 200:
                raise ValueError('PDF超过200页，请拆分或补充重点文字。')
            pages, missing = [], False
            for index, page in enumerate(reader.pages, 1):
                content = page.get_contents()
                if content and len(content.get_data()) > 10 * 1024 * 1024:
                    raise ValueError('PDF单页内容过大，请拆分或补充重点文字。')
                value = page.extract_text() or ''
                if value.strip():
                    pages.append(f'【第{index}页】\n{value}')
                else:
                    missing = True
                if sum(len(p) for p in pages) > MAX_TEXT:
                    incomplete = index < len(reader.pages)
                    break
            text = '\n\n'.join(pages)
            if missing:
                error = '部分PDF页面未提取到文字，扫描图片内容尚未识别；请结合原件核对。'
        elif suffix in ('.txt', '.md', '.csv'):
            for encoding in ('utf-8-sig', 'utf-16', 'gb18030'):
                try:
                    text = raw.decode(encoding)
                    if '\x00' in text:
                        raise UnicodeError()
                    break
                except UnicodeError:
                    text = ''
            if not text:
                raise ValueError('文字编码未识别，请另存UTF-8文本。')
        else:
            raise ValueError('原件已保留；图片需要文字识别，旧Office文件请另存docx/pptx/xlsx或粘贴文字。')
        text = text.strip()
        if not text:
            raise ValueError('原件已保留，未提取到可读正文；扫描件请补充可复制文字。')
    except Exception as exc:
        error = str(exc) if isinstance(exc, ValueError) else '原件已保留，正文暂未能读取；可另存标准格式或补充文字。'
        text = ''
    truncated = incomplete or len(text) > MAX_TEXT
    return {'text': text[:MAX_TEXT], 'parse_status': 'ready' if text else 'needs_text',
            'parse_error': error[:400], 'truncated': truncated,
            'warning': '正文较长，已读取前50万字；其余内容仍保留在原文件。' if truncated else ''}


def persist_document(db, owner, material_id, data):
    db.execute('INSERT INTO crm_document_files(owner,material_id,request_id,filename,sha256,size,content,parse_status,parse_error,truncated,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)',
               (owner, material_id, data['request_id'], data['filename'], data['sha256'], len(data['content']),
                data['content'], data['parse_status'], data['parse_error'], int(data['truncated']), data['created_at']))
    if data['parse_status'] != 'ready':
        db.execute("UPDATE crm_materials SET status='failed',error=? WHERE owner=? AND id=?", (data['parse_error'], owner, material_id))
        db.execute("UPDATE crm_material_jobs SET status='failed' WHERE owner=? AND material_id=?", (owner, material_id))


class DocumentAttachmentService:
    MAX_BYTES = MAX_BYTES
    def __init__(self, crm, materials, *, clock=time.time):
        self.crm, self.materials, self.clock = crm, materials, clock
        with crm._transaction() as db:
            db.execute('CREATE TABLE IF NOT EXISTS crm_document_files(owner TEXT NOT NULL,material_id INTEGER NOT NULL,request_id TEXT NOT NULL,filename TEXT NOT NULL,sha256 TEXT NOT NULL,size INTEGER NOT NULL,content BLOB NOT NULL,parse_status TEXT NOT NULL,parse_error TEXT NOT NULL,truncated INTEGER NOT NULL DEFAULT 0,created_at REAL NOT NULL,PRIMARY KEY(owner,material_id),UNIQUE(owner,request_id),FOREIGN KEY(owner,material_id) REFERENCES crm_materials(owner,id))')

    def upload(self, owner, name, raw, request_id, *, parsed=None):
        owner, name = _owner(owner), filename(name)
        request_id = _text(request_id, '上传标识', 200, required=True)
        if not isinstance(raw, bytes) or not raw or len(raw) > MAX_BYTES:
            raise ValueError('文件不能为空，且每份不能超过20MB。')
        digest = hashlib.sha256(raw).hexdigest()
        with self.crm._lock:
            prior = self.crm._db.execute('SELECT * FROM crm_document_files WHERE owner=? AND request_id=?', (owner, request_id)).fetchone()
            if prior:
                if prior['sha256'] != digest or prior['filename'] != name:
                    raise ValueError('上传标识已经保存另一份文件，请重新选择文件。')
                return {'material': self.materials.detail(owner, prior['material_id'])['material'], 'attachment': self.public(prior)}
        parsed = parsed or extract_document(name, raw)
        metadata = {**parsed, 'request_id': request_id, 'filename': name, 'content': raw,
                    'sha256': digest, 'created_at': self.clock()}
        text = parsed['text'] or f'【附件正文尚未识别】文件：{name}。请补充可复制文字；此占位不代表文件实际内容。'
        material = self.materials.enqueue(owner, {'provider': 'manual', 'title': name[:120], 'text': text, 'category': 'memo'},
            namespace='document:' + request_id, document=metadata)
        return {'material': material, 'attachment': self.public(self.get(owner, material['id']))}

    def get(self, owner, material_id):
        with self.crm._lock:
            row = self.crm._db.execute('SELECT * FROM crm_document_files WHERE owner=? AND material_id=?', (_owner(owner), _identifier(material_id))).fetchone()
            if row is None:
                raise KeyError('未找到你的附件原件。')
            return dict(row)

    @staticmethod
    def public(row):
        return {**{key: row[key] for key in ('material_id', 'filename', 'size', 'parse_status', 'parse_error', 'truncated')},
                'download_url': f'/api/materials/{row["material_id"]}/file'}

    def text_corrected(self, owner, material_id):
        with self.crm._transaction() as db:
            db.execute("UPDATE crm_document_files SET parse_status='ready',parse_error='',truncated=0 WHERE owner=? AND material_id=?", (_owner(owner), _identifier(material_id)))

    def finish_parse(self, owner, material_id, parsed):
        # A manual correction made while parsing takes precedence. The original
        # bytes and upload identity were committed before background parsing.
        with self.crm._lock:
            file = self.get(owner, material_id)
            if file['parse_status'] != 'parsing':
                return
            if parsed['text']:
                workspace, project_id = None, None
                if self.crm._db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='crm_opportunity_links'").fetchone():
                    from .sales_workspace import SalesWorkspace
                    workspace = SalesWorkspace(self.crm,clock=self.clock)
                    link = self.crm._db.execute("SELECT * FROM crm_opportunity_links WHERE owner=? AND entity_type='material' AND entity_id=?",(owner,material_id)).fetchone()
                    old = workspace._entity(self.crm._db,owner,'material',material_id)
                    if link and link['source_snapshot']==workspace._link_snapshot(self.crm._db,owner,'material',old):
                        project_id=link['opportunity_id']
                material = self.materials.detail(owner, material_id)['material']
                self.materials.update(owner, material_id, {'revision':material['revision'], 'text':parsed['text']}, _text_source='DOCUMENT')
                if project_id:
                    try:
                        # Initial extraction completes the original uploaded file;
                        # preserve only an already valid explicit project choice.
                        workspace.link(owner,'material',material_id,project_id)
                    except (KeyError,ValueError):
                        pass  # Archived or changed scopes remain subject to review.
            with self.crm._transaction() as db:
                db.execute('UPDATE crm_document_files SET parse_status=?,parse_error=?,truncated=? WHERE owner=? AND material_id=?',
                    (parsed['parse_status'],parsed['parse_error'],int(parsed['truncated']),owner,material_id))
                if not parsed['text']:
                    db.execute("UPDATE crm_materials SET status='failed',error=? WHERE owner=? AND id=?", (parsed['parse_error'],owner,material_id))
