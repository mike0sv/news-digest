from copy import deepcopy
from hashlib import sha256
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

from digest import providers
from digest.feeds import article_id, canonical_url, parse_feed, parse_html_source, plain_text
from digest.pipeline import (generate_issue, load_config, prepare, read_json, select_candidates,
                             validate_output, write_json, recent_history)
from digest.publish import build, deliver, issue_html, telegram_text

NOW = datetime(2026, 11, 23, 10, tzinfo=timezone.utc)
ROOT = Path(__file__).resolve().parents[1]


def article(id='a', group='agents', source='src', **extra):
    return {'id': id, 'title': f'Release {id}', 'url': f'https://example.org/{id}',
            'source': 'Example', 'source_id': source, 'group': group,
            'published_at': NOW.isoformat(), 'discovered_at': NOW.isoformat(),
            'text': 'A documented product change.', 'text_kind': 'feed', **extra}


def response(ids=None):
    return {'intro': 'Обзор', 'stories': [{'title': 'Новый релиз', 'summary': 'Конкретные изменения.',
            'why_it_matters': 'Полезно разработчикам.', 'group': 'agents',
            'article_ids': ids or ['a'], 'is_update': False, 'important': False,
            'importance_reason': '', 'previous_story_refs': []}]}


class FeedTests(unittest.TestCase):
    def test_tracking_is_removed_but_meaningful_query_is_preserved(self):
        self.assertEqual(canonical_url('https://EXAMPLE.org/x?utm_source=x&id=2#top'),
                         'https://example.org/x?id=2')
        self.assertEqual(article_id('https://example.org/x?utm_campaign=1'),
                         article_id('https://example.org/x'))

    def test_rss_feed_parses_content_and_publication_time(self):
        xml = '<rss><channel><item><title>A &amp; B</title><link>https://example.org/a</link><pubDate>Mon, 23 Nov 2026 09:00:00 GMT</pubDate><description><![CDATA[<p>Hello <b>world</b></p><script>bad()</script>]]></description></item></channel></rss>'
        items = parse_feed(xml, {'id':'x','name':'X','url':'https://example.org/feed','group':'agents'}, NOW)
        self.assertEqual(items[0]['text'], 'Hello world')
        self.assertEqual(items[0]['published_at'], '2026-11-23T09:00:00+00:00')

    def test_atom_prefers_alternate_over_self(self):
        xml = '<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>Release</title><link rel="self" href="https://example.org/api"/><link rel="alternate" href="/release"/><updated>2026-11-23T09:00:00Z</updated></entry></feed>'
        items = parse_feed(xml, {'id':'x','name':'X','url':'https://example.org/feed','group':'agents'}, NOW)
        self.assertEqual(items[0]['url'], 'https://example.org/release')

    def test_updated_govuk_document_has_a_new_identity_at_the_same_url(self):
        template = '<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>Rules</title><link href="https://www.gov.uk/rules"/><published>2026-11-01T09:00:00Z</published><updated>{}</updated></entry></feed>'
        source={'id':'ukvi','name':'GOV.UK','url':'https://www.gov.uk/feed','group':'uk','track_updates':True}
        first=parse_feed(template.format('2026-11-22T09:00:00Z'),source,NOW)[0]
        updated=parse_feed(template.format('2026-11-23T09:00:00Z'),source,NOW)[0]
        self.assertEqual(first['url'],updated['url'])
        self.assertNotEqual(first['id'],updated['id'])
        self.assertEqual(updated['published_at'],'2026-11-23T09:00:00+00:00')

    def test_entity_declarations_rejected(self):
        with self.assertRaises(ValueError):
            parse_feed('<!DOCTYPE rss [<!ENTITY x "y">]><rss/>', {}, NOW)

    def test_javascript_article_links_rejected(self):
        xml = '<rss><channel><item><title>X</title><link>javascript:alert(1)</link></item></channel></rss>'
        self.assertEqual(parse_feed(xml, {'url':'https://example.org/feed'}, NOW), [])

    def test_html_listing_limits_to_matching_same_origin_articles(self):
        listing = '<a href="https://evil.org/news/fake">Fake</a><a href="/about">About</a><a href="/news/release">News</a>'
        page = '<meta property="article:published_time" content="2026-11-23T09:00:00Z"><main><h1>Real release</h1><p>Details.</p></main>'
        with patch('digest.feeds.http', return_value=page) as fetch:
            items = parse_html_source(listing, {'id':'x','name':'X','url':'https://example.org/news',
                                      'group':'agents','link_pattern':r'/news/[^/]+$'}, NOW)
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(items[0]['title'], 'Real release')
        self.assertIn('Details.', items[0]['text'])


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root/'config').mkdir()
        (self.root/'config/settings.json').write_text((ROOT/'config/settings.json').read_text())
        self.config = load_config(self.root)
        self.config["provider"]="links"
        self.config["telegram"]["instant_view"]=False
        write_json(self.root/"config/settings.json",self.config)
        (self.root/'web').mkdir()
        (self.root/'web/index.html').write_text('<h1>Test</h1>')

    def tearDown(self):
        self.temp.cleanup()

    def prepare(self, articles=None, **kwargs):
        with patch('digest.pipeline.collect', return_value=(articles or [article()],
                   [{'id':'src','name':'Example','ok':True,'count':1,'error':None}])):
            return prepare(self.root, now=NOW, **kwargs)

    def delivery_receipts(self, chat='1'):
        key = sha256(chat.lower().encode()).hexdigest()
        return read_json(self.root/'data/deliveries.json')['destinations'][key]

    def test_switching_to_channel_sends_once_without_reusing_personal_message(self):
        self.prepare(); issue = generate_issue(self.root)
        issue.update(provider='gemini', date=datetime.now(timezone.utc).date().isoformat())
        issue['id'] = issue['date']
        write_json(self.root/'data/issues'/f"{issue['id']}.json", issue)
        legacy = {issue['id']: {'message_id': 80, 'hash': 'legacy'}}
        write_json(self.root/'data/deliveries.json', legacy)
        with patch.dict('os.environ', {'TELEGRAM_BOT_TOKEN':'secret','TELEGRAM_CHAT_ID':'1'}), patch(
                'digest.publish.post_json', return_value={'ok':True,'result':{'message_id':7}}) as post:
            deliver(self.root)
            with patch.dict('os.environ', {'TELEGRAM_CHAT_ID':'@news'}):
                deliver(self.root); deliver(self.root)
            with patch.dict('os.environ', {'TELEGRAM_CHAT_ID':'@NEWS'}):
                deliver(self.root)
            deliver(self.root)
        self.assertEqual(post.call_count, 2)
        self.assertEqual([c.args[1]['chat_id'] for c in post.call_args_list], ['1','@news'])
        self.assertTrue(all(c.args[0].endswith('/sendMessage') for c in post.call_args_list))
        self.assertEqual(read_json(self.root/'data/deliveries.json')[issue['id']], legacy[issue['id']])
        self.assertEqual(self.delivery_receipts('@news')[issue['id']]['message_id'], 7)

    def test_importance_allows_zero_and_rejects_more_than_three(self):
        _, stories = validate_output(response(), [article()], self.config)
        self.assertFalse(stories[0]['important'])
        output = response()
        output['stories'] = []
        items = [article(str(i)) for i in range(4)]
        for a in items:
            story = response([a['id']])['stories'][0]
            story.update(important=True, importance_reason='Substantial usable capability')
            output['stories'].append(story)
        with self.assertRaisesRegex(ValueError, 'At most 3'):
            validate_output(output, items, self.config)
        output['stories'].pop()
        self.assertEqual(len(validate_output(output, items, self.config)[1]), 3)

    def test_importance_reason_must_match_the_marker(self):
        output = response(); output['stories'][0]['important'] = True
        with self.assertRaises(ValueError): validate_output(output, [article()], self.config)
        output['stories'][0].update(important=False, importance_reason='Pretending importance')
        with self.assertRaises(ValueError): validate_output(output, [article()], self.config)

    def test_update_references_must_resolve_to_real_history(self):
        history = [{'ref':'2026-11-22/old','issue_id':'2026-11-22','story_id':'old',
                    'date':'2026-11-22','title':'Prior event'}]
        output=response(); output['stories'][0].update(is_update=True, previous_story_refs=['invented'])
        with self.assertRaises(ValueError): validate_output(output,[article()],self.config,history)
        output['stories'][0]['previous_story_refs']=['2026-11-22/old']
        story=validate_output(output,[article()],self.config,history)[1][0]
        self.assertEqual(story['previous_stories'][0]['story_id'],'old')
        output['stories'][0]['is_update']=False
        with self.assertRaises(ValueError): validate_output(output,[article()],self.config,history)

    def test_history_excludes_same_day_and_future_issues(self):
        for day in ['2026-11-22','2026-11-23','2026-11-24']:
            write_json(self.root/'data/issues'/f'{day}.json',
                {'id':day,'date':day,'stories':[{'id':'s','title':day,'summary':'x','group':'agents'}]})
        history=recent_history(self.root,NOW,14)
        self.assertEqual([s['date'] for s in history],['2026-11-22'])
        self.assertEqual(history[0]['ref'],'2026-11-22/s')

    def test_historical_filter_excludes_discoveries_and_future_articles(self):
        config={**self.config,'historical':True,'lookback_hours':24}
        items=[article('dated'),article('undated',published_at=None),
               article('snapshot',date_kind='discovery'),
               article('future',published_at=(NOW+timedelta(seconds=1)).isoformat())]
        self.assertEqual([a['id'] for a in select_candidates(items,{'selected':{}},config,NOW)],['dated'])

    def test_future_date_fails_before_source_fetch(self):
        with patch('digest.signals.collect_historical') as fetch:
            with self.assertRaises(ValueError): prepare(self.root,now=NOW,date='2026-11-24')
        fetch.assert_not_called()

    def test_retrospective_preserves_live_state_and_never_sends(self):
        live={'version':1,'articles':{'future':article('future')},'selected':{'future':'2026-11-23'}}
        write_json(self.root/'data/state.json',live)
        write_json(self.root/'data/status.json',{'last_success':'live'})
        past=NOW-timedelta(days=1)
        with patch('digest.signals.collect_historical',return_value=(
                [article('past',published_at=past.isoformat())],[{'ok':True}])):
            prepare(self.root,now=NOW,date='2026-11-22')
        issue=generate_issue(self.root)
        self.assertTrue(issue['retrospective'])
        self.assertEqual(read_json(self.root/'data/state.json')['selected']['future'],'2026-11-23')
        self.assertIn('future',read_json(self.root/'data/state.json')['articles'])
        self.assertEqual(read_json(self.root/'data/status.json')['last_success'],'live')
        with patch.dict('os.environ',{'TELEGRAM_BOT_TOKEN':'s','TELEGRAM_CHAT_ID':'1'}), patch('digest.publish.post_json') as post:
            deliver(self.root)
        post.assert_not_called()

    def test_permalink_and_markdown_link_to_previous_card(self):
        self.prepare(); issue=generate_issue(self.root)
        story=issue['stories'][0]
        story['previous_stories']=[{'issue_id':'2026-11-22','story_id':'older','date':'2026-11-22','title':'Prior'}]
        html=issue_html(issue)
        self.assertIn('2026-11-22.html#story-older',html)
        self.assertIn('id="story-'+story['id']+'"',html)

    def test_telegraph_publishes_full_text_once_and_edits_changed_content(self):
        from digest.instant_view import publish, content
        self.config['telegram']['instant_view']=True
        write_json(self.root/'config/settings.json',self.config)
        self.prepare(); issue=generate_issue(self.root)
        nodes=content(issue,'https://example.org/')
        self.assertIn(issue['stories'][0]['summary'],json.dumps(nodes,ensure_ascii=False))
        result={'ok':True,'result':{'url':'https://telegra.ph/Issue-11-23','path':'Issue-11-23'}}
        with patch.dict('os.environ',{'TELEGRAPH_ACCESS_TOKEN':'private'}), patch('digest.instant_view.post_json',return_value=result) as post:
            publish(self.root); publish(self.root)
            self.assertEqual(post.call_count,1)
            issue['intro']='Changed';write_json(self.root/'data/issues/2026-11-23.json',issue)
            publish(self.root)
            self.assertTrue(post.call_args.args[0].endswith('/editPage'))
        self.assertNotIn('private',(self.root/'data/telegraph.json').read_text())
        self.assertIn('Instant View',telegram_text(issue,'https://example.org/',result['result']['url']))

    def test_github_release_query_omits_assets_and_future_releases(self):
        from digest.signals import github_releases
        source={'id':'codex','name':'Codex','url':'https://github.com/openai/codex/releases.atom','group':'agents'}
        rows=[{'name':'Release','tagName':'v1','url':'https://github.com/openai/codex/releases/tag/v1',
               'publishedAt':NOW.isoformat(),'description':'Real release notes','isDraft':False},
              {'name':'Future','tagName':'v2','url':'https://github.com/openai/codex/releases/tag/v2',
               'publishedAt':(NOW+timedelta(days=1)).isoformat(),'description':'Future','isDraft':False}]
        with patch.dict('os.environ',{'GITHUB_TOKEN':'private'}), patch('digest.signals.http',return_value=json.dumps(
                {'data':{'repository':{'releases':{'nodes':rows}}}})) as request:
            items=github_releases(source,NOW)
        self.assertEqual(len(items),1)
        self.assertEqual(items[0]['text'],'Real release notes')
        self.assertNotIn('releaseAssets',request.call_args.kwargs['payload']['query'])
        self.assertEqual(request.call_args.kwargs['headers']['Authorization'],'Bearer private')

    def test_source_specific_budget_keeps_discovery_from_crowding_out_releases(self):
        config=deepcopy(self.config)
        config['sources'].append({'id':'discovery','candidate_limit':2})
        items=[article(str(i),source='discovery') for i in range(10)]+[article('release',source='codex')]
        selected=select_candidates(items,{'selected':{}},config,NOW)
        self.assertEqual(sum(a['source_id']=='discovery' for a in selected),2)
        self.assertIn('release',[a['id'] for a in selected])

    def test_changed_delivered_issue_edits_original_telegram_message(self):
        self.prepare();issue=generate_issue(self.root)
        issue.update(provider='gemini',date=datetime.now(timezone.utc).date().isoformat())
        issue['id']=issue['date'];path=self.root/'data/issues'/f"{issue['id']}.json"
        write_json(path,issue)
        with patch.dict('os.environ',{'TELEGRAM_BOT_TOKEN':'secret','TELEGRAM_CHAT_ID':'1'}), patch(
                'digest.publish.post_json',return_value={'ok':True,'result':{'message_id':7}}) as post:
            deliver(self.root)
            issue['stories'][0]['title']='Edited headline';write_json(path,issue)
            deliver(self.root);deliver(self.root)
        self.assertEqual(post.call_count,2)
        self.assertTrue(post.call_args.args[0].endswith('/editMessageText'))
        self.assertEqual(post.call_args.args[1]['message_id'],7)

    def test_already_selected_and_old_articles_are_excluded(self):
        old = article('old', published_at=(NOW-timedelta(days=5)).isoformat())
        future = article('future', published_at=(NOW+timedelta(days=1)).isoformat())
        selected = select_candidates([article(), old, future, article('fresh')],
                                     {'selected':{'a':'2026-11-22'}}, self.config, NOW)
        self.assertEqual([a['id'] for a in selected], ['fresh'])

    def test_source_caps_and_group_budgets_prevent_flooding(self):
        items = [article(str(i)) for i in range(40)] + [article('uk', group='uk', source='ukvi')]
        selected = select_candidates(items, {'selected':{}}, self.config, NOW)
        self.assertEqual(len([a for a in selected if a['source_id']=='src']), 8)
        self.assertIn('uk', [a['id'] for a in selected])

    def test_unknown_model_citation_blocks_publication(self):
        self.prepare()
        path = self.root/'model.json'
        write_json(path, response(['invented']))
        with self.assertRaises(ValueError): generate_issue(self.root, response_file=path)
        self.assertFalse((self.root/'data/state.json').exists())
        self.assertFalse((self.root/'data/issues').exists())

    def test_same_article_cannot_appear_twice(self):
        output = response()
        output['stories'].append(deepcopy(output['stories'][0]))
        with self.assertRaises(ValueError): validate_output(output, [article()], self.config)

    def test_valid_citations_are_resolved_by_application(self):
        intro, stories = validate_output(response(), [article()], self.config)
        self.assertEqual(stories[0]['sources'][0]['url'], 'https://example.org/a')
        self.assertNotIn('article_ids', stories[0])

    def test_links_mode_persists_metadata_and_skips_same_day_rerun(self):
        self.prepare()
        issue = generate_issue(self.root)
        state = read_json(self.root/'data/state.json')
        self.assertNotIn('text', state['articles']['a'])
        self.assertIn('a', state['selected'])
        with patch('digest.pipeline.collect') as fetch:
            job = prepare(self.root, now=NOW)
        self.assertFalse(job['ready'])
        fetch.assert_not_called()
        self.assertEqual(issue['provider'], 'links')

    def test_schedule_observes_london_summer_time(self):
        write_json(self.root/'config/settings.json', {**self.config, 'morning_hour': 8})
        # 07:17 UTC is 08:17 London in summer, but too early in winter.
        summer = NOW.replace(month=7,hour=7,minute=17)
        winter = NOW.replace(hour=7,minute=17)
        with patch('digest.pipeline.collect', return_value=([article()], [{'ok':True}])) as fetch:
            job = prepare(self.root, now=winter, scheduled=True)
            self.assertFalse(job['ready'])
            fetch.assert_not_called()
            job = prepare(self.root, now=summer, scheduled=True)
            self.assertTrue(job['ready'])

    def test_morning_recovery_creates_one_issue_in_winter_and_summer(self):
        write_json(self.root/'config/settings.json', {**self.config, 'morning_hour': 8})
        for month, hour in [(11,8),(7,7)]:
            morning=NOW.replace(month=month,hour=hour,minute=17)
            first_attempt=morning+timedelta(minutes=30)  # Primary trigger was missed.
            item=article('recovery-'+str(month),published_at=morning.isoformat())
            with patch('digest.pipeline.collect',return_value=([item],[{'ok':True}])) as fetch:
                early=prepare(self.root,now=morning-timedelta(hours=1),scheduled=True)
                self.assertFalse(early['ready']);fetch.assert_not_called()
                job=prepare(self.root,now=first_attempt,scheduled=True)
                self.assertTrue(job['ready'])
                issue=generate_issue(self.root)
                self.assertEqual(issue['date'],morning.date().isoformat())
                self.assertEqual(len(issue['stories']),1)
            with patch('digest.pipeline.collect') as fetch:
                for delay in [60,210]:
                    job=prepare(self.root,now=morning+timedelta(minutes=delay),scheduled=True)
                    self.assertFalse(job['ready'])
                fetch.assert_not_called()

    def test_overnight_schedule_uses_london_day_and_accepts_previous_day_news(self):
        for month in [11, 7]:
            local = datetime(2026, month, 23, 0, 49, tzinfo=ZoneInfo('Europe/London'))
            now = local.astimezone(timezone.utc)
            item = article('overnight-'+str(month),
                           published_at=(now-timedelta(hours=5)).isoformat())
            with patch('digest.pipeline.collect', return_value=([item], [{'ok':True}])):
                job = prepare(self.root, now=now, scheduled=True)
                self.assertTrue(job['ready'])
                issue = generate_issue(self.root)
            self.assertEqual(issue['date'], local.date().isoformat())
            self.assertEqual(len(issue['stories']), 1)
            with patch('digest.pipeline.collect') as fetch:
                retry = prepare(self.root, now=now+timedelta(hours=8), scheduled=True)
                self.assertFalse(retry['ready'])
                fetch.assert_not_called()

    def test_all_source_failures_do_not_write_state(self):
        with patch('digest.pipeline.collect', return_value=([],[{'ok':False}])):
            with self.assertRaises(RuntimeError): prepare(self.root, now=NOW)
        self.assertFalse((self.root/'data').exists())

    def test_model_failure_has_no_implicit_paid_fallback(self):
        self.config['provider']='gemini'
        write_json(self.root/'config/settings.json', self.config)
        self.prepare()
        with patch('digest.providers.generate', side_effect=RuntimeError('quota')) as generate:
            with self.assertRaises(RuntimeError): generate_issue(self.root)
        self.assertEqual(generate.call_count,1)
        self.assertFalse((self.root/'data/issues').exists())

    def test_invalid_citations_get_one_same_provider_repair(self):
        self.config['provider']='gemini'
        write_json(self.root/'config/settings.json', self.config)
        self.prepare()
        invalid = providers.Result(response(['invented']), {})
        valid = providers.Result(response(['A1']), {})
        with patch('digest.providers.generate', side_effect=[invalid,valid]) as generate:
            issue = generate_issue(self.root)
        self.assertEqual(generate.call_count,2)
        self.assertEqual(issue['stories'][0]['sources'][0]['id'],'a')

    def test_telegram_waits_for_configuration_without_sending(self):
        self.prepare(); generate_issue(self.root)
        with patch.dict('os.environ',{},clear=True), patch('digest.publish.post_json') as post:
            deliver(self.root)
        post.assert_not_called()
        self.assertFalse((self.root/'data/deliveries.json').exists())

    def test_delivery_receipt_prevents_a_normal_repeat(self):
        import digest.publish
        self.prepare(); issue=generate_issue(self.root)
        issue['provider']='gemini'
        issue['date']=datetime.now(timezone.utc).date().isoformat()
        issue['id']=issue['date']
        write_json(self.root/'data/issues'/f"{issue['id']}.json",issue)
        with patch.dict('os.environ',{'TELEGRAM_BOT_TOKEN':'secret','TELEGRAM_CHAT_ID':'1'}), patch('digest.publish.post_json',return_value={'ok':True,'result':{'message_id':7}}) as post:
            deliver(self.root); deliver(self.root)
        self.assertEqual(post.call_count,1)
        self.assertEqual(self.delivery_receipts()[issue['id']]['message_id'],7)
        self.assertNotIn('delivery_uncertain',self.delivery_receipts()[issue['id']])

    def test_telegram_timeout_blocks_automatic_resend_without_marking_success(self):
        self.prepare(); issue=generate_issue(self.root)
        issue['provider']='gemini'
        issue['date']=datetime.now(timezone.utc).date().isoformat()
        issue['id']=issue['date']
        write_json(self.root/'data/issues'/f"{issue['id']}.json",issue)
        with patch.dict('os.environ',{'TELEGRAM_BOT_TOKEN':'secret','TELEGRAM_CHAT_ID':'1'}), patch('digest.publish.post_json',side_effect=RuntimeError('timeout')) as post:
            with self.assertRaises(RuntimeError): deliver(self.root)
            with self.assertRaisesRegex(RuntimeError,'uncertain outcome'): deliver(self.root)
        self.assertEqual(post.call_count,1)
        receipt=self.delivery_receipts()[issue['id']]
        self.assertTrue(receipt['delivery_uncertain'])
        self.assertNotIn('hash',receipt);self.assertNotIn('message_id',receipt)

    def test_confirmed_telegram_rejection_can_be_retried(self):
        self.prepare();issue=generate_issue(self.root)
        issue.update(provider='gemini',date=datetime.now(timezone.utc).date().isoformat())
        issue['id']=issue['date']
        write_json(self.root/'data/issues'/f"{issue['id']}.json",issue)
        with patch.dict('os.environ',{'TELEGRAM_BOT_TOKEN':'secret','TELEGRAM_CHAT_ID':'1'}), patch(
                'digest.publish.post_json',side_effect=[{'ok':False},{'ok':True,'result':{'message_id':8}}]) as post:
            with self.assertRaises(RuntimeError):deliver(self.root)
            self.assertFalse(self.delivery_receipts()[issue['id']]['delivery_uncertain'])
            deliver(self.root);deliver(self.root)
        self.assertEqual(post.call_count,2)
        receipt=self.delivery_receipts()[issue['id']]
        self.assertEqual(receipt['message_id'],8)
        self.assertNotIn('delivery_uncertain',receipt)

    def test_static_export_excludes_state_preferences_and_credentials(self):
        self.prepare(); generate_issue(self.root); build(self.root)
        public = read_json(self.root/'site/archive.json')
        self.assertNotIn('instructions', public)
        self.assertNotIn('state', public)
        self.assertFalse((self.root/'site/state.json').exists())
        self.assertTrue((self.root/'site/issues/2026-11-23.html').exists())

    def test_html_permalink_escapes_model_content(self):
        self.prepare(); issue = generate_issue(self.root)
        issue['stories'][0]['title']='<script>alert(1)</script>'
        html = issue_html(issue)
        self.assertNotIn('<script>', html)
        self.assertIn('&lt;script&gt;', html)

    def test_telegram_preserves_all_groups_and_escapes_model_html(self):
        self.prepare(); issue=generate_issue(self.root)
        issue['stories']=[]
        for group in issue['groups']:
            story={'title':'<script>bad</script>', 'summary':'x'*1000, 'group':group['id'],
                   'sources':[{'url':'https://example.org/'+('x'*300)}]}
            issue['stories'] += [deepcopy(story) for _ in range(3)]
        message=telegram_text(issue,'https://example.org/')
        for group in issue['groups']: self.assertIn(group['name'],message)
        self.assertEqual(message.count('&lt;script&gt;'),len(issue['stories']))
        self.assertLessEqual(len(plain_text(message)),3500)
        self.assertNotIn('<script>',message)

    def test_telegram_message_is_bounded_and_retains_archive_link(self):
        self.prepare(); issue = generate_issue(self.root)
        story = deepcopy(issue['stories'][0]); story['title']='x'*200; story['summary']='y'*1000
        issue['stories'] = [story]*20
        message = telegram_text(issue, 'https://example.org/')
        self.assertLessEqual(len(plain_text(message)),3500)
        self.assertIn('?issue=2026-11-23',message)


class ProviderTests(unittest.TestCase):
    def setUp(self):
        self.config = json.loads((ROOT/'config/settings.json').read_text())
        self.schema = providers.schema_for(self.config['groups'])

    @patch.dict('os.environ', {'GEMINI_API_KEY':'secret'})
    def test_gemini_normalizes_and_authenticates_in_header(self):
        data = {'candidates':[{'finishReason':'STOP','content':{'parts':[{'text':json.dumps(response())}]}}]}
        with patch('digest.providers.post_json',return_value=data) as post:
            result=providers.gemini('prompt',self.schema,'test-model')
        self.assertEqual(result.data['intro'],'Обзор')
        self.assertNotIn('secret',post.call_args.args[0])
        self.assertEqual(post.call_args.args[2]['x-goog-api-key'],'secret')

    @patch.dict('os.environ', {'ANTHROPIC_API_KEY':'secret'})
    def test_anthropic_normalizes_tool_output(self):
        data={'content':[{'type':'tool_use','name':'submit_digest','input':response()}]}
        with patch('digest.providers.post_json',return_value=data):
            self.assertEqual(providers.anthropic('p',self.schema,'model').data,response())

    @patch.dict('os.environ', {'OPENAI_API_KEY':'secret'})
    def test_openai_normalizes_responses_and_refuses_incomplete(self):
        data={'status':'completed','output':[{'type':'message','content':[{'type':'output_text','text':json.dumps(response())}]}]}
        with patch('digest.providers.post_json',return_value=data):
            self.assertEqual(providers.openai('p',self.schema,'model').data,response())
        data['status']='incomplete'
        with patch('digest.providers.post_json',return_value=data):
            with self.assertRaises(ValueError): providers.openai('p',self.schema,'model')

    def test_claude_cli_has_no_tools_and_does_not_receive_telegram_token(self):
        import subprocess
        completed = subprocess.CompletedProcess([],0,json.dumps({'structured_output':response()}),'')
        with patch.dict('os.environ', {'TELEGRAM_BOT_TOKEN':'secret'}), patch('digest.providers.subprocess.run',return_value=completed) as run:
            self.assertEqual(providers.cli('p',self.schema,'haiku','claude-cli').data,response())
        command=run.call_args.args[0]
        self.assertEqual(command[command.index('--tools')+1],'')
        self.assertNotIn('TELEGRAM_BOT_TOKEN',run.call_args.kwargs['env'])

    def test_cli_failure_diagnostics_never_repeat_output_or_secrets(self):
        import subprocess
        for output, expected in [('Invalid API key: private-value','authentication rejected'),
                                 ('error: unknown option --flag private-value','configured option'),
                                 ('Unexpected private-value','unclassified CLI failure'),
                                 ('This model does not support the effort parameter. private-value','effort setting'),
                                 ('Unexpected effort counter: private-value','unclassified CLI failure'),
                                 ('Failed to authenticate. API Error: 401 OAuth access token is invalid. private-value','authentication rejected'),
                                 (json.dumps({'api_error_status':401,'result':'private-value'}),'HTTP 401')]:
            process=subprocess.CompletedProcess([],1,output,'')
            diagnostic=providers.cli_error(process,'claude-cli')
            self.assertIn(expected,diagnostic)
            self.assertNotIn('private-value',diagnostic)

    def test_claude_rejects_pasting_an_export_command_instead_of_token(self):
        with patch.dict('os.environ',{'CLAUDE_CODE_OAUTH_TOKEN':'export CLAUDE_CODE_OAUTH_TOKEN=secret'}), patch('digest.providers.subprocess.run') as run:
            with self.assertRaisesRegex(ValueError,'only the token'):
                providers.cli('p',self.schema,'haiku','claude-cli')
        run.assert_not_called()

    def test_codex_reads_final_output_instead_of_progress(self):
        import subprocess
        def execute(command, **kwargs):
            Path(command[command.index('-o')+1]).write_text(json.dumps(response()))
            self.assertIn('read-only',command)
            self.assertIn('shell_tool',command)
            return subprocess.CompletedProcess(command,0,'progress is not JSON','')
        with patch('digest.providers.subprocess.run',side_effect=execute):
            self.assertEqual(providers.cli('p',self.schema,'','codex').data,response())

    def test_missing_secret_fails_without_network(self):
        with patch.dict('os.environ',{},clear=True), patch('digest.providers.post_json') as post:
            with self.assertRaises(ValueError): providers.gemini('p',self.schema,'model')
        post.assert_not_called()


if __name__=='__main__': unittest.main()
