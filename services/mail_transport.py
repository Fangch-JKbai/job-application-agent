"""Read-only IMAP over certificate-verified TLS. No SMTP or attachment parsing."""
from datetime import datetime,timedelta
from zoneinfo import ZoneInfo
from email import policy
from email.parser import BytesParser
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
import imaplib
import re
import ssl
from schemas.mailboxes import HOSTS
from services.errors import ServiceError

class MailFailure(ServiceError):status_code=502
class TextHTML(HTMLParser):
    def __init__(self):super().__init__();self.parts=[];self.hidden=0
    def handle_starttag(self,tag,attrs):
        if tag in {'script','style'}:self.hidden+=1
        if tag in {'br','p','div','li','tr'}:self.parts.append('\n')
    def handle_endtag(self,tag):
        if tag in {'script','style'}:self.hidden=max(0,self.hidden-1)
        if tag in {'p','div','li','tr'}:self.parts.append('\n')
    def handle_data(self,data):
        if not self.hidden:self.parts.append(data)

def decode_message(raw):
    msg=BytesParser(policy=policy.default).parsebytes(raw)
    body=msg.get_body(preferencelist=('plain','html'))
    text=body.get_content() if body else ''
    if body and body.get_content_type()=='text/html':
        parser=TextHTML();parser.feed(text);text=''.join(parser.parts)
    text=re.sub(r'\n[ \t]*\n+', '\n\n',text).strip()
    sent=None
    if msg['Date']:
        try:sent=parsedate_to_datetime(str(msg['Date']))
        except (ValueError,TypeError):pass  # Malformed Date stays unknown, never replaced by arrival time.
        if sent and not sent.tzinfo:sent=None
    return {'subject':str(msg['Subject'] or ''),'sender':str(msg['From'] or ''),'sent_at':sent,'text':text}

# A transparent prefilter, not a claim of full-mail classification accuracy.
RECRUITMENT=re.compile(r'秋招|春招|校招|校园招聘|实习|笔试|测评|面试|应聘|投递|录用|入职|招聘|offer|interview|assessment|recruit|written[ -]?test',re.I)
NON_NOTICE=re.compile(r'验证码|邮箱认证|绑定.*邮箱|邮箱.*绑定|安全提醒|登录提醒|新设备登录|诈骗|笔试经验|面试经验|面试攻略|求职攻略|subscription|billing',re.I)
def candidate(message):
    # Operational/security mail is not a hiring notice, even when its footer mentions recruitment.
    if NON_NOTICE.search(message['subject']):return False
    return bool(RECRUITMENT.search(message['subject']+'\n'+message['sender']+'\n'+message['text']))

def read_mail(provider,address,code,known_validity=None,last_uid=0,limit=30,since=None):
    try:
        with imaplib.IMAP4_SSL(HOSTS[provider],993,ssl_context=ssl.create_default_context(),timeout=20) as box:
            box.login(address,code)
            # NetEase requires RFC 2971 client identification before selecting INBOX.
            if provider in {'163','126'}:
                imaplib.Commands.setdefault('ID',('AUTH',))
                status,_=box._simple_command('ID','("name" "job-application-agent" "version" "0.1.0" "vendor" "personal")')
                if status!='OK':raise MailFailure('网易邮箱客户端标识失败，请检查IMAP设置')
            status,_=box.select('INBOX',readonly=True)
            if status!='OK':raise MailFailure('邮箱收件箱不可读，请检查IMAP服务与授权码')
            validity=box.response('UIDVALIDITY')[1][0].decode('ascii')
            since=since or (datetime.now(ZoneInfo('Asia/Shanghai')).date()-timedelta(days=60))
            stamp=f'{since.day:02d}-'+['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'][since.month-1]+f'-{since.year}'
            criteria=['SINCE',stamp]
            if known_validity==validity:criteria+=['UID',f'{last_uid+1}:*']
            status,data=box.uid('search',None,*criteria)
            if status!='OK':raise MailFailure('邮箱查询失败')
            ids=sorted(int(v) for v in data[0].split())
            if known_validity==validity:ids=[v for v in ids if v>last_uid][:limit]
            else:ids=ids[:limit]  # Oldest first, cursor advances through the entire 60-day initial range.
            messages=[]
            for uid in ids:
                status,parts=box.uid('fetch',str(uid),'(RFC822.SIZE BODY.PEEK[HEADER])')
                if status!='OK':raise MailFailure('读取邮件头失败')
                metadata=next((part[0] for part in parts if isinstance(part,tuple)),b'')
                size=re.search(rb'RFC822.SIZE (\d+)',metadata)
                if not size:raise MailFailure('邮箱未返回邮件大小')
                if int(size.group(1))>512000:
                    header=next(part[1] for part in parts if isinstance(part,tuple))
                    message=decode_message(header);message['text']='[邮件超过500KB，请在邮箱查看后手动粘贴正文；未读取附件]'
                    message['processing_error']='邮件过大，未自动读取正文'
                else:
                    status,parts=box.uid('fetch',str(uid),'(BODY.PEEK[])')
                    if status!='OK':raise MailFailure('读取邮件正文失败')
                    raw=next(part[1] for part in parts if isinstance(part,tuple))
                    message=decode_message(raw)
                    if len(message['text'])>55000:
                        message['text']='[正文超过55000字符，请在邮箱查看后手动粘贴]';message['processing_error']='正文过长，未自动识别'
                messages.append({'uid':str(uid),**message})
            return validity,messages
    except (imaplib.IMAP4.error,OSError) as error:
        # Provider errors may contain address/token: don't propagate their text.
        raise MailFailure('邮箱连接或认证失败，请检查网络、IMAP开关及客户端授权码') from None
