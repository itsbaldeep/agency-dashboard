import unittest
from unittest import mock
import app as dashboard

class MarketingCalendarTests(unittest.TestCase):
    def setUp(self):
        dashboard.app.config.update(TESTING=True)
        self.client=dashboard.app.test_client()
        self.data={'month':'2026-11','timezone':'Asia/Kolkata','today':'2026-11-17','brands':[{'id':31,'name':'TrueApply'}],'events':[],'unscheduled':[],'coverage':{'complete':True}}

    def test_visual_calendar_and_json_share_filters(self):
        with mock.patch.object(dashboard.models,'db') as db, mock.patch('calendar_events.load_calendar',return_value=self.data) as load:
            r=self.client.get('/calendar?brand_id=31&month=2026-11&timezone=UTC')
            self.assertEqual(r.status_code,200)
            self.assertIn('id="mc-grid"',r.text)
            self.assertIn('Help articles',r.text)
            self.assertIn('Projected window',r.text)
            load.assert_called_with(db.return_value,brand_id=31,month='2026-11',timezone_name='UTC')
            r=self.client.get('/api/marketing-calendar?brand_id=31')
            self.assertEqual(r.json,self.data)
            self.assertEqual(db.return_value.close.call_count,2)

    def test_invalid_filters_do_not_open_database(self):
        with mock.patch.object(dashboard.models,'db') as db:
            for path in ['/calendar','/api/marketing-calendar']:
                for args in [{'brand_id':x} for x in ('0','²','2147483648','-1','abc')]+[{'month':'2026-13'},{'month':'0000-01'},{'timezone':'Nope'}]:
                    with self.subTest(path=path,args=args):
                        self.assertEqual(self.client.get(path,query_string=args).status_code,400)
            db.assert_not_called()

    def test_embedded_event_text_cannot_break_out_of_json(self):
        self.data['events']=[{'title':'</script><script>alert(1)</script>','date':'2026-11-17','status':'published'}]
        with mock.patch.object(dashboard.models,'db'),mock.patch('calendar_events.load_calendar',return_value=self.data):
            r=self.client.get('/calendar')
        self.assertNotIn('</script><script>alert(1)',r.text)
        self.assertIn('\\u003c/script\\u003e',r.text)

if __name__=='__main__':unittest.main()
