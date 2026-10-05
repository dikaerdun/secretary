"""Optional short-audio transcription and owner-specific vocabulary.

Qwen ASR protocol: https://www.alibabacloud.com/help/zh/model-studio/qwen-asr-api-reference
Audio stays in memory; an unavailable provider never produces a simulated transcript.
"""
import asyncio
import base64
import json
import time
from urllib.parse import urlsplit

import httpx

from .crm import _owner

DEFAULT_HOTWORDS = ['数据安全', '商用密码', '密评', '等保', '国密', 'SM2', 'SM3', 'SM4',
                    '密钥管理', '密码机', '数据库加密', '数据脱敏', '零信任', 'POC']


class AudioUnavailable(ValueError):
    pass


class AudioService:
    MAX_BYTES = 6 * 1024 * 1024  # Base64 plus context stays below provider's 10MB limit.
    FORMATS = {'audio/wav', 'audio/x-wav', 'audio/mpeg', 'audio/mp3', 'audio/mp4',
               'audio/x-m4a', 'audio/aac', 'audio/ogg', 'audio/flac', 'audio/webm', 'video/webm'}

    def __init__(self, crm, api_key='', base_url='https://dashscope.aliyuncs.com/compatible-mode/v1',
                 model='qwen3-asr-flash', *, transport=None):
        self.crm, self.api_key, self.base_url, self.model = crm, api_key, base_url.rstrip('/'), model
        parsed = urlsplit(self.base_url)
        if parsed.scheme != 'https' or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError('语音识别服务地址需要使用有效 HTTPS 地址。')
        if model != 'qwen3-asr-flash' and not model.startswith('qwen3-asr-flash-'):
            raise ValueError('语音识别模型需要使用 qwen3-asr-flash 系列。')
        self.transport = transport
        self.slots = asyncio.Semaphore(1)
        with crm._lock:
            crm._db.execute('CREATE TABLE IF NOT EXISTS crm_voice_settings '
                            '(owner TEXT PRIMARY KEY,hotwords_json TEXT NOT NULL,updated_at REAL NOT NULL)')

    def capabilities(self):
        return {'configured': bool(self.api_key), 'can_transcribe': bool(self.api_key),
                'provider': '阿里云百炼 Qwen ASR' if self.api_key else None,
                'reason': '' if self.api_key else '尚未配置专业语音识别服务；可先用企业微信语音转写或手机键盘语音输入，核对文字后整理。',
                'max_bytes': self.MAX_BYTES, 'max_seconds': 300, 'supported_formats': sorted(self.FORMATS),
                'hotwords_scope': '本后台的专业转写；不会修改企业微信自带语音识别'}

    def settings(self, owner):
        owner = _owner(owner)
        with self.crm._lock:
            row = self.crm._db.execute('SELECT hotwords_json FROM crm_voice_settings WHERE owner=?', (owner,)).fetchone()
            words = json.loads(row[0]) if row else list(DEFAULT_HOTWORDS)
            # Current names are collected at request time, so new customers and aliases
            # improve future transcription without manual vocabulary maintenance.
            names = []
            for row in self.crm._db.execute('SELECT name,aliases_json FROM crm_customers WHERE owner=? ORDER BY updated_at DESC LIMIT 100', (owner,)):
                names.append(row['name'])
                names.extend(json.loads(row['aliases_json']))
            if self.crm._db.execute("SELECT 1 FROM sqlite_master WHERE name='crm_contacts'").fetchone():
                names.extend(row[0] for row in self.crm._db.execute('SELECT name FROM crm_contacts WHERE owner=? AND archived=0 LIMIT 100', (owner,)))
        return {'hotwords': words, 'automatic_hotwords': list(dict.fromkeys(names))[:100]}

    def save_settings(self, owner, data, now):
        owner = _owner(owner)
        if not isinstance(data, dict) or set(data) != {'hotwords'} or not isinstance(data['hotwords'], list):
            raise ValueError('请提交热词列表。')
        if len(data['hotwords']) > 100:
            raise ValueError('自定义热词最多 100 个。')
        if any(not isinstance(word, str) or not word.strip() or len(word) > 60 or any(ord(c) < 32 for c in word)
               for word in data['hotwords']):
            raise ValueError('每个热词需要 1 至 60 个可见字符。')
        words = list(dict.fromkeys(word.strip() for word in data['hotwords']))
        with self.crm._transaction() as db:
            db.execute('INSERT INTO crm_voice_settings VALUES (?,?,?) ON CONFLICT(owner) '
                       'DO UPDATE SET hotwords_json=excluded.hotwords_json,updated_at=excluded.updated_at',
                       (owner, json.dumps(words, ensure_ascii=False), now))
        return self.settings(owner)

    async def transcribe(self, owner, raw, mime):
        if not self.api_key:
            raise AudioUnavailable(self.capabilities()['reason'])
        if not raw or len(raw) > self.MAX_BYTES:
            raise ValueError('音频文件需要大于零且不超过 6 MB，录音最长 5 分钟。')
        mime = mime.split(';')[0].lower()
        if mime not in self.FORMATS:
            raise ValueError('请选择 WAV、MP3、M4A、AAC、OGG、FLAC 或 WebM 音频。')
        # Browser WebM/Opus is converted to the documented WAV input. This requires
        # ffmpeg only when that format is actually submitted to an enabled provider.
        if mime in ('audio/webm', 'video/webm'):
            import shutil
            import subprocess
            ffmpeg = shutil.which('ffmpeg')
            if not ffmpeg:
                raise ValueError('服务器尚未安装录音格式转换工具，请上传 WAV 或 MP3 音频。')
            def convert():
                try:
                    result = subprocess.run([ffmpeg, '-v', 'error', '-protocol_whitelist', 'pipe',
                        '-i', 'pipe:0', '-t', '301', '-progress', 'pipe:2', '-nostats',
                        '-f', 'mp3', '-ac', '1', '-ar', '16000', '-b:a', '128k', 'pipe:1'], input=raw,
                        capture_output=True, timeout=30, check=True)
                    import re
                    duration = max((int(value) for value in re.findall(rb'out_time_us=(\d+)', result.stderr)), default=0)
                    if duration > 300_100_000:
                        raise ValueError('录音超过 5 分钟，请拆分后重新上传；本次没有提交截断的音频。')
                    return result.stdout
                except (OSError, subprocess.SubprocessError):
                    raise ValueError('录音格式转换失败，请保留原音频并重新上传。') from None
            raw = await asyncio.to_thread(convert)
            mime = 'audio/mpeg'
            if len(raw) > self.MAX_BYTES:
                raise ValueError('转换后的录音过长，请拆成更短的片段。')
        vocabulary = self.settings(owner)
        words = list(dict.fromkeys(vocabulary['hotwords'] + vocabulary['automatic_hotwords']))
        payload = {'model': self.model, 'stream': False, 'asr_options': {'enable_itn': True},
            'messages': [{'role': 'system', 'content': '数据安全与商用密码客户拜访记录。实体词表：' + '、'.join(words)[:5000]},
                         {'role': 'user', 'content': [{'type': 'input_audio', 'input_audio':
                             {'data': 'data:' + mime + ';base64,' + base64.b64encode(raw).decode()}}]}]}
        started = time.monotonic()
        async with self.slots:
            try:
                async with httpx.AsyncClient(timeout=90, transport=self.transport, follow_redirects=False) as client:
                    result = await client.post(self.base_url + '/chat/completions',
                        headers={'Authorization': 'Bearer ' + self.api_key}, json=payload)
                    result.raise_for_status()
                    choice = result.json()['choices'][0]
                    if choice.get('finish_reason') != 'stop':
                        raise ValueError()
                    text = choice['message']['content']
                if not isinstance(text, str) or not text.strip() or len(text) > 6000:
                    raise ValueError()
            except Exception:
                raise AudioUnavailable('这次转写未完成，请保留原音频后重试，或用键盘语音输入。') from None
        return {'text': text.strip(), 'elapsed_seconds': round(time.monotonic() - started, 1),
                'needs_review': True, 'message': '转写完成，请核对客户姓名、金额和日期后再整理。'}
