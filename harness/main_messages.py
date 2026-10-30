"""Project-wide policy for agent messages to any currently bound main session."""
import json

from .common import require


def main_message_policy(state):
    # No work filter: one current policy covers every managed main in this store.
    # A missing event retains historical behavior; an unreadable policy raises.
    row = state.db.execute("SELECT seq,payload FROM events WHERE kind='main_message_policy' ORDER BY seq DESC LIMIT 1").fetchone()
    if not row:
        return None
    policy = json.loads(row['payload'])
    require(isinstance(policy, dict) and policy.get('mode') == 'user_initiated',
            'Current main message policy is unavailable or unsupported', 'MAIN_MESSAGE_POLICY_UNAVAILABLE')
    return {'mode': policy['mode'], 'event_seq': row['seq']}


def check_main_message(state, target):
    """All send routes share this check; caller, urgency and approval labels grant no exception."""
    binding = state.binding(target) if target else None
    if binding and binding['role'] == 'main' and main_message_policy(state):
        require(False, 'Main sessions are user initiated: agent messages and wakeups to managed main sessions are disabled; '
                'save results locally for the user to review', 'MAIN_MESSAGE_USER_INITIATED')
