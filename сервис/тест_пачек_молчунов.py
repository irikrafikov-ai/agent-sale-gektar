"""Regression cases for premature closures found in the live 08.10 audit."""
import sys
import types
import unittest
from unittest.mock import patch
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
for name in ('интеграции','интеграции.avito','интеграции.bitrix','интеграции.telegram'):
    sys.modules.setdefault(name,types.ModuleType(name))
sys.modules['интеграции.avito'].Avito=object
sys.modules['интеграции.avito'].AvitoError=Exception
sys.modules['интеграции.bitrix'].Bitrix=object
sys.modules['интеграции.telegram'].Telegram=object
import молчуны

def out(t, text='Текст'):
    return {'direction':'out','author_id':84069402,'created':t,'type':'text','content':{'text':text}}

class TouchCounting(unittest.TestCase):
    def test_yana_seven_blocks_are_five_touches(self):
        ms=[out(1000),out(1001),out(1001)]+[out(1000+d*86400) for d in (3,5,11,14)]
        r=молчуны.разобрать_чат(list(reversed(ms)),84069402)
        self.assertEqual((r['наши'],r['касания'],r['клиента']),(7,5,0))

    def test_maxim_deleted_entry_is_not_a_touch(self):
        ms=[out(1000),out(1001),out(1001),out(7000,'Сообщение удалено')]+[out(1000+d*86400) for d in (3,14,24)]
        r=молчуны.разобрать_чат(ms,84069402)
        self.assertEqual((r['наши'],r['касания']),(6,4))

    def test_seven_distinct_days_reach_threshold(self):
        r=молчуны.разобрать_чат([out(1000+d*86400) for d in range(7)],84069402)
        self.assertEqual(r['касания'],7)
        self.assertTrue(r['хронология_полная'])

    def test_unknown_timestamp_cannot_authorize_closure(self):
        r=молчуны.разобрать_чат([out(0)]+[out(1000+d*86400) for d in range(7)],84069402)
        self.assertFalse(r['хронология_полная'])

    def test_system_and_deleted_reply_do_not_make_buyer_active(self):
        ms=[out(1000),{'direction':'in','author_id':1,'created':1001,'type':'system'},
            {'direction':'in','author_id':99,'created':1002,'type':'deleted'}]
        self.assertEqual(молчуны.разобрать_чат(ms,84069402)['клиента'],0)

    def test_real_reply_resets_group_and_preserves_response(self):
        ms=[out(1000),{'direction':'in','author_id':99,'created':1001,'type':'text'},out(1002)]
        r=молчуны.разобрать_чат(ms,84069402)
        self.assertEqual((r['касания'],r['клиента']),(2,1))

    def test_group_window_uses_first_message_not_rolling_gap(self):
        r=молчуны.разобрать_чат([out(1000),out(1500),out(2000)],84069402)
        self.assertEqual(r['касания'],2)

    def test_history_limit_is_not_silently_complete(self):
        class API:
            def chat_messages(self,*args,**kwargs):return [out(1000)]*100
        with self.assertRaises(RuntimeError):молчуны.все_сообщения(API(),'chat',предел=100)

class ClosingSafety(unittest.TestCase):
    def run_closure(self,messages,stage='EXECUTING',personal=False,sibling=None):
        class Avito:
            user_id=84069402
            def chat_messages(self,chat,*args,**kwargs):return sibling if chat=='sibling' else messages
            def close(self):pass
        class Bitrix:
            updates=[]
            def crm_list(self,*args,**kwargs):return [{'ID':'123','STAGE_ID':stage}]
            def crm_get(self,*args):return {'ID':'123','STAGE_ID':stage,'TITLE':'Клиент','COMMENTS':''}
            def call(self,method,params):
                assert method=='crm.timeline.comment.list'
                if params['start']==0:return [{'COMMENT':'Обычная запись'}]*50
                return [{'COMMENT':'#ЛИЧНОЕ_ВЕДЕНИЕ_ИРИКА'}] if personal else []
            def crm_update(self,*args):self.updates.append(args)
            def timeline_comment_add(self,*args):pass
            def close(self):pass
        b=Bitrix();b.updates=[]
        finding={'хронология_полная':True,'касания':7,'клиента':0,'кабинет':'gektar','chat_id':'chat','название':'Берега','объявление':'Участок','имя':'Клиент','наши':7,'наше_последнее':'01.10.2026'}
        if sibling is not None:finding['связанные_чаты']=['sibling']
        with patch.object(молчуны,'Avito',lambda **kw:Avito()),patch.object(молчуны,'Bitrix',lambda:b),patch.object(молчуны.реестр,'настроен',lambda cab:True),patch.dict(молчуны.os.environ,{'AVITO_CLIENT_ID':'test','AVITO_CLIENT_SECRET':'test'}):
            молчуны.забраковать([finding])
        return b.updates

    def test_new_reply_between_find_and_close_blocks_write(self):
        ms=[out(1000+d*86400) for d in range(7)]+[{'direction':'in','author_id':99,'created':1000+8*86400,'type':'text'}]
        self.assertEqual(self.run_closure(ms),[])

    def test_fresh_count_below_threshold_blocks_write(self):
        self.assertEqual(self.run_closure([out(1000+d*86400) for d in range(5)]),[])

    def test_won_card_is_not_reclosed(self):
        self.assertEqual(self.run_closure([out(1000+d*86400) for d in range(7)],stage='WON'),[])

    def test_personal_handoff_on_second_timeline_page_blocks_write(self):
        self.assertEqual(self.run_closure([out(1000+d*86400) for d in range(7)],personal=True),[])

    def test_verified_seven_touches_can_close(self):
        self.assertEqual(len(self.run_closure([out(1000+d*86400) for d in range(7)])),1)

    def test_reply_in_another_chat_of_same_buyer_blocks_write(self):
        reply=[{'direction':'in','author_id':99,'created':1000,'type':'text'}]
        self.assertEqual(self.run_closure([out(1000+d*86400) for d in range(7)],sibling=reply),[])

if __name__=='__main__':unittest.main()
