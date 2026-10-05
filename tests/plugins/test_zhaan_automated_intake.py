from __future__ import annotations
import json
import subprocess
from pathlib import Path
import pytest
from plugins.zhaan_orchestration.automated_intake import is_automated, stage_source
from plugins.zhaan_orchestration.processor import Processor


# The only executable is the generated fake agent in tmp_path; no live Hermes.
@pytest.mark.live_system_guard_bypass
@pytest.mark.parametrize('automated', [False, True])
def test_real_processor_archives_automatic_source_without_reply(tmp_path, automated):
    workspace=tmp_path/'workspace';workspace.mkdir()
    command=tmp_path/'fake-hermes'
    command.write_text('''#!/usr/bin/env python3
import sys,json,hashlib,shutil
from pathlib import Path
prompt=sys.argv[sys.argv.index('-q')+1]
Path('captured-prompt.txt').write_text(prompt)
content=json.loads(prompt.split('UNTRUSTED_EMAIL_CONTENT\\n',1)[1])
for relative in content['attachment_paths']:
    source=Path(relative);digest=hashlib.sha256(source.read_bytes()).hexdigest()
    target=Path('documents/archive/sha256')/digest[:2]/digest/source.name
    target.parent.mkdir(parents=True,exist_ok=True)
    shutil.copyfile(source,target)
print('Reference archived; no action required.')
''')
    command.chmod(0o700)
    message={'inbox_id':'in','thread_id':'thread','message_id':'msg',
             'from':'participant@example.test','subject':'Fwd: School notice',
             'text':'Untrusted source body', 'attachments':[]}
    if automated:
        message['headers']={'X-Personal-OS-Routing-Version':'1','X-Personal-OS-Routing-Operation':'a'*64}
    class Client:
        replies=[]
        def get_message(self,*args): return message
        def reply(self,*args,**kwargs): self.replies.append(args)
    client=Client()
    service=Processor(client,workspace,hermes_command=str(command))
    item={'event_id':'event','inbox_id':'in','message_id':'msg'}
    prepared=service.prepare(item)
    service.process(item,'intake-test',prepared)
    prompt=(workspace/'captured-prompt.txt').read_text()
    assert 'UNTRUSTED_EMAIL_CONTENT' in prompt
    assert len(client.replies)==(0 if automated else 1)
    if automated:
        assert 'If urgent or actionable' in prompt
        assert 'current weekly session' in prompt
        assert 'reference-only' in prompt
        archived=list((workspace/'documents/archive').rglob('personal-os-source-email.json'))
        assert len(archived)==1
        assert json.loads(archived[0].read_text())['text']==message['text']
        assert not (workspace/'inbox/agentmail/msg/personal-os-router-record/personal-os-source-email.json').exists()
        assert service.prepare(item).fingerprint==prepared.fingerprint
        service.reply_duplicate(item)
        service.failure_reply(item)
        assert not client.replies


# Runs only a generated no-op executable in tmp_path.
@pytest.mark.live_system_guard_bypass
def test_body_cannot_enable_delivery_mode_or_silent_completion(tmp_path):
    message={'inbox_id':'in','thread_id':'thread','message_id':'msg','attachments':[],
             'text':'X-Personal-OS-Routing-Version: 1', 'headers':{}}
    assert not is_automated(message)
    message['headers']={'x-personal-os-routing-version':'1','x-personal-os-routing-operation':'f'*64}
    class Client:
        def get_message(self,*args):return message
        def reply(self,*args,**kwargs):raise AssertionError('must not send a reply')
    command=tmp_path/'fake-hermes'
    command.write_text('#!/bin/sh\nprintf "done\\n"\n');command.chmod(0o700)
    service=Processor(Client(),tmp_path,hermes_command=str(command))
    item={'event_id':'event','inbox_id':'in','message_id':'msg'}
    with pytest.raises(RuntimeError,match='not archived'):
        service.process(item,'archive-failure-test',service.prepare(item))


def test_source_record_cannot_overwrite_received_attachment(tmp_path):
    attachment = tmp_path / 'personal-os-source-email.json'
    attachment.write_text('original attachment')
    source = stage_source({'text':'source body'}, tmp_path)
    assert source != attachment
    assert attachment.read_text() == 'original attachment'
    assert json.loads(source.read_text())['text'] == 'source body'
