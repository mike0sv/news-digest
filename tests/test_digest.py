from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from digest import providers
from digest.feeds import article_id, canonical_url, parse_feed, parse_html_source, plain_text
from digest.pipeline import (generate_issue, load_config, prepare, read_json, select_candidates,
                             validate_output, write_json)
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
            'article_ids': ids or ['a'], 'is_update': False}]}


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
        (self.root/'web').mkdir()
        (self.root/'web/index.html').write_text('<h1>Test</h1>')

    def tearDown(self):
        self.temp.cleanup()

    def prepare(self, articles=None, **kwargs):
        with patch('digest.pipeline.collect', return_value=(articles or [article()],
                   [{'id':'src','name':'Example','ok':True,'count':1,'error':None}])):
            return prepare(self.root, now=NOW, **kwargs)

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
        # 07:17 UTC is 08:17 London in summer, but too early in winter.
        summer = NOW.replace(month=7,hour=7,minute=17)
        winter = NOW.replace(hour=7,minute=17)
        with patch('digest.pipeline.collect', return_value=([article()], [{'ok':True}])) as fetch:
            job = prepare(self.root, now=winter, scheduled=True)
            self.assertFalse(job['ready'])
            fetch.assert_not_called()
            job = prepare(self.root, now=summer, scheduled=True)
            self.assertTrue(job['ready'])

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
        self.assertEqual(read_json(self.root/'data/deliveries.json')[issue['id']]['message_id'],7)

    def test_telegram_failure_does_not_mark_delivery(self):
        self.prepare(); issue=generate_issue(self.root)
        issue['provider']='gemini'
        issue['date']=datetime.now(timezone.utc).date().isoformat()
        issue['id']=issue['date']
        write_json(self.root/'data/issues'/f"{issue['id']}.json",issue)
        with patch.dict('os.environ',{'TELEGRAM_BOT_TOKEN':'secret','TELEGRAM_CHAT_ID':'1'}), patch('digest.publish.post_json',side_effect=RuntimeError('timeout')):
            with self.assertRaises(RuntimeError): deliver(self.root)
        self.assertFalse((self.root/'data/deliveries.json').exists())

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

    def test_telegram_message_is_bounded_and_retains_archive_link(self):
        self.prepare(); issue = generate_issue(self.root)
        story = deepcopy(issue['stories'][0]); story['title']='x'*200; story['summary']='y'*1000
        issue['stories'] = [story]*20
        message = telegram_text(issue, 'https://example.org/')
        self.assertLessEqual(len(message),3500)
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
