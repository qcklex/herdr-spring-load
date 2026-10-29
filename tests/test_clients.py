"""Session binding and the one-client gate (review M1, L6)."""
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from spring_load import clients

ENV = {'HOME': '/h', 'XDG_CONFIG_HOME': '/h/c'}

def client(pid, argv=('herdr',), env=ENV, tty='/dev/ttys001'):
    return {'pid': pid, 'argv': list(argv), 'tty': tty, 'env': None if env is None else dict(env)}

class GateTest(unittest.TestCase):
    def test_unknown_environment_is_never_bound(self):
        with self.assertRaises(clients.Unbound):
            clients.bind(client(1, env=None), {'HERDR_SOCKET_PATH': '/elsewhere.sock', **ENV})
        with self.assertRaises(clients.Unbound):
            clients.bind(client(1, argv=('herdr', '--session', 'foo'), env=None), ENV)

    def test_unknown_environment_counts_against_the_gate(self):
        hovered = clients.bind(client(1), {})
        others = [client(1), client(2, env=None)]
        self.assertEqual(clients.attached_count(hovered, others, {}), 2)
        session = [client(1), client(3, env={**ENV, 'HERDR_SESSION': 'foo'})]
        self.assertEqual(clients.attached_count(hovered, session, {}), 1, 'a known other session does not count')
        self.assertEqual(clients.attached_count(hovered, [client(1), client(4, tty=None)], {}), 1, 'no tty: not a client')
        self.assertEqual(clients.attached_count(hovered, [client(1), client(5, argv=('herdr', 'server'), env=None)], {}), 1)

    def test_core_environment_is_ignored(self):
        bound = clients.bind(client(1), {'HERDR_SOCKET_PATH': '/pane/herdr.sock'})
        self.assertEqual(bound.api_socket, Path('/h/c/herdr/herdr.sock'))

    def test_ps_args_with_spaces_in_the_path(self):
        self.assertEqual(clients.split_ps_args('/Users/a b/bin/herdr --session work'),
                         ['/Users/a b/bin/herdr', '--session', 'work'])
        self.assertEqual(clients.split_ps_args('herdr'), ['herdr'])
        self.assertEqual(clients.split_ps_args('/bin/zsh -l'), ['/bin/zsh', '-l'])
        self.assertEqual(clients.classify(clients.split_ps_args('/opt/my tools/herdr session attach x')), ('local', 'x'))

if __name__ == '__main__':
    unittest.main()
