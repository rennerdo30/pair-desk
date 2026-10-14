"""Report clarification and agent verification, using only throwaway desks."""
import json
import subprocess
import sys

from helpers import ROOT, StoreCase
from test_api import ApiCase
from test_mcp_cli import McpCase
from pair_desk.cli import session_summary
from pair_desk.context import save_link
from pair_desk.store import Invalid, Store
from pair_desk.mcp import TOOLS


class UpdateToolTests(McpCase):
    def test_update_all_fields_and_preserve_owner_words(self):
        with Store(self.tmp) as s:
            s.create_issue('mygame', {'title': 'weird door', 'body': 'stuck here', 'source': 'owner'})
        fields = {'title': 'Door blocks the player', 'body': 'Steps and expected result', 'kind': 'task',
                  'priority': 'p1', 'area': 'doors', 'tags': ['collision'], 'size': 'M',
                  'milestone': 'Beta', 'location': {'seed': 42, 'place': 'harbor'},
                  'commands': [{'command': '/goto 1 2', 'label': 'door'}]}
        err, result = self.call(1, 'update_issue', {'id': 'MG-1', **fields, 'author': 'owner'})
        self.assertFalse(err, result)
        err, detail = self.call(2, 'get_issue', {'id': 'MG-1'})
        for k in ('title', 'body', 'kind', 'priority', 'area', 'tags', 'size', 'milestone'):
            self.assertEqual(detail[k], fields[k])
        self.assertEqual(detail['location']['seed'], 42)
        self.assertEqual(detail['location']['commands'], fields['commands'])
        edited = [a for a in detail['activity'] if a['action'] == 'edited'][-1]
        self.assertEqual(edited['detail']['owner_original'], {'title': 'weird door', 'body': 'stuck here'})
        self.call(3, 'update_issue', {'id': 'MG-1', 'body': 'More precise steps'})
        _, detail = self.call(4, 'get_issue', {'id': 'MG-1'})
        edits = [a for a in detail['activity'] if a['action'] == 'edited']
        self.assertEqual(sum('owner_original' in a['detail'] for a in edits), 1)
        self.assertEqual(edits[-1]['detail']['changes']['body']['from'], fields['body'])
        before = len(detail['activity'])
        self.call(5, 'update_issue', {'id': 'MG-1', 'body': 'More precise steps'})
        _, detail = self.call(6, 'get_issue', {'id': 'MG-1'})
        self.assertEqual(len(detail['activity']), before)

    def test_permissions_validation_and_milestone_schemas(self):
        for name in ('create_issue', 'update_issue'):
            schema = next(t for t in TOOLS if t['name'] == name)['inputSchema']
            self.assertIn('milestone', schema['properties'])
        err, issue = self.call(1, 'create_issue', {'title': 'Check', 'milestone': 'Alpha', 'status': 'auto_check'})
        self.assertFalse(err, issue)
        for changes in ({'status': 'passed'}, {'status': 'auto_check'}, {'source': 'owner'},
                        {'kind': 'invalid'}, {'title': ''}, {'milestone': 'x' * 121}, {}):
            err, result = self.call(2, 'update_issue', {'id': issue['id'], **changes})
            self.assertTrue(err, result)
        err, result = self.call(3, 'update_issue', {'id': issue['id'], 'milestone': '', 'size': ''})
        self.assertFalse(err, result)
        _, detail = self.call(4, 'get_issue', {'id': issue['id']})
        self.assertEqual(detail['milestone'], '')
        for name in ('set_status', 'create_issue'):
            args = {'id': issue['id'], 'title': 'No', 'status': 'passed', 'author': 'owner'}
            self.assertTrue(self.call(5, name, args)[0])

    def test_agent_queue_and_manual_handover(self):
        _, i = self.call(1, 'create_issue', {'title': 'fix', 'kind': 'task'})
        self.call(2, 'set_plan', {'id': i['id'], 'steps': ['Implement']})
        err, done = self.call(3, 'update_step', {'id': i['id'], 'index': 1, 'state': 'done'})
        self.assertFalse(err, done)
        self.assertEqual(done['status'], 'auto_check')
        _, queue = self.call(4, 'list_issues', {'status': 'auto_check'})
        self.assertEqual(queue['status_counts']['auto_check'], 1)
        self.assertEqual(len(queue['issues']), 1)
        self.assertTrue(self.call(5, 'set_status', {'id': i['id'], 'status': 'to_check'})[0])
        self.call(6, 'update_issue', {'id': i['id'], 'command': '/goto 1 2'})
        self.assertFalse(self.call(7, 'set_status', {'id': i['id'], 'status': 'to_check'})[0])
        self.assertFalse(self.call(8, 'set_status', {'id': i['id'], 'status': 'auto_check'})[0])
        self.call(9, 'comment', {'id': i['id'], 'text': 'Tests pass; no manual review required by project rules.'})
        self.assertFalse(self.call(10, 'set_status', {'id': i['id'], 'status': 'closed'})[0])


class UpdateStoreTests(StoreCase):
    def test_existing_data_reopens_without_status_reassignment(self):
        s = self.store
        s.create_issue('mygame', {'title': 'Old waiting check', 'status': 'to_check'})
        s.create_issue('mygame', {'title': 'Old owner report', 'body': 'original'})
        s.conn.execute("UPDATE meta SET value='5' WHERE key='schema'")
        s.close()
        self.store = Store(self.tmp)
        self.assertEqual(self.store.get_issue('MG-1')['status'], 'to_check')
        self.assertEqual(self.store.get_issue('MG-2')['body'], 'original')
        self.assertEqual(self.store.conn.execute("SELECT value FROM meta WHERE key='schema'").fetchone()[0], '6')
        self.store.set_status('MG-2', 'auto_check', 'agent')
        self.assertEqual(self.store.list_projects()[0]['counts']['auto_check'], 1)

    def test_owner_edits_and_agent_edits_are_distinct(self):
        s = self.store
        s.create_issue('mygame', {'title': 'Owner words', 'body': 'body'})
        s.update_issue('MG-1', {'title': 'Owner revision'}, actor='owner')
        self.assertNotIn('owner_original', s.get_issue('MG-1')['activity'][-1]['detail'])
        s.update_issue('MG-1', {'body': 'Agent clarification'}, actor='agent')
        self.assertEqual(s.get_issue('MG-1')['activity'][-1]['detail']['owner_original']['title'], 'Owner revision')
        with self.assertRaises(Invalid):
            s.update_issue('MG-1', {'status': 'passed'}, actor='agent')
        with self.assertRaises(Invalid):
            s.update_issue('MG-1', {'status': ' PASSED '}, actor='agent')
        self.assertEqual(s.set_status('MG-1', 'passed', 'owner')['status'], 'passed')

    def test_rework_leaves_automatic_queue_and_returns_after_last_step(self):
        s = self.store
        s.create_issue('mygame', {'title': 'fix', 'status': 'auto_check'})
        s.set_plan('MG-1', ['Rework', 'Test'], actor='agent')
        self.assertEqual(s.update_step('MG-1', 1, 'doing', actor='agent')['status'], 'in_progress')
        s.update_step('MG-1', 1, 'done', actor='agent')
        self.assertEqual(s.update_step('MG-1', 2, 'done', actor='agent')['status'], 'auto_check')
        s.set_status('MG-1', 'to_check')
        s.set_plan('MG-1', ['Rework', 'Test', 'Adjust'], actor='agent')
        self.assertEqual(s.update_step('MG-1', 3, 'done', actor='agent')['status'], 'auto_check')

    def test_cli_edit_status_and_session_summary(self):
        self.store.create_issue('mygame', {'title': 'short', 'body': 'words'})
        repo = self.tmp / 'repo'
        repo.mkdir()
        save_link(self.tmp, repo, 'mygame')
        for args in [('edit', 'MG-1', '--title', 'Clear report', '--body', 'Reproduction', '--milestone', 'Beta'),
                     ('status', 'MG-1', 'auto_check')]:
            run = subprocess.run([sys.executable, str(ROOT / 'desk.py'), '--data', str(self.tmp), *args],
                                 capture_output=True, text=True)
            self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIn('1 auto_check waiting for agent verification', session_summary(self.tmp, str(repo)))
        detail = self.store.get_issue('MG-1')
        self.assertEqual(detail['milestone'], 'Beta')
        self.assertEqual(detail['activity'][1]['detail']['owner_original']['body'], 'words')


class UpdateHttpTests(ApiCase):
    def test_patch_report_and_auto_check(self):
        self.store.create_project('mygame', 'MyGame', 'MG')
        self.store.create_issue('mygame', {'title': 'short', 'body': 'owner words'})
        code, detail, _ = self.req('PATCH', '/api/issues/MG-1', {'title': 'Precise report', 'body': 'Steps',
                                'milestone': 'Beta', 'status': 'auto_check', 'actor': 'agent'})
        self.assertEqual(code, 200)
        self.assertEqual(detail['status'], 'auto_check')
        self.assertEqual(detail['activity'][-1]['detail']['owner_original']['body'], 'owner words')
        code, result, _ = self.req('PATCH', '/api/issues/MG-1', {'status': 'passed', 'actor': 'agent'})
        self.assertEqual(code, 400)
        code, result, _ = self.req('GET', '/api/projects/mygame/issues?status=auto_check')
        self.assertEqual(result['counts']['status']['auto_check'], 1)
