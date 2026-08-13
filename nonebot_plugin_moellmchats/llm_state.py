from collections import deque

from .config import config_parser
from .state_store import BoundedDequeStore

context_dict = BoundedDequeStore(
    lambda: config_parser.get_config("max_group_history", 10)
)
token_usage_history = deque(maxlen=50)
