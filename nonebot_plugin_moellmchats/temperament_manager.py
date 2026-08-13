from pathlib import Path
from traceback import format_exc

from nonebot.log import logger
import nonebot_plugin_localstore as store
import ujson as json

config_path: Path = store.get_plugin_config_dir()


# 性格设置类
class TemperamentManager:
    def __init__(self):
        self.temperament_config = Path(
            config_path / "temperament_config.json"
        )
        self.temperaments_path = Path(config_path / "temperaments.json")
        self.temperaments = self.read_temperaments()
        self.temperament_dict = self.read_temperament()

    def load_candidate(self) -> tuple[dict, dict]:
        """Parse both resources without changing the active generation."""
        with open(self.temperaments_path, encoding="utf-8") as f:
            temperaments = json.load(f)
        with open(self.temperament_config, encoding="utf-8") as f:
            assignments = json.load(f)
        if not isinstance(temperaments, dict) or not temperaments:
            raise ValueError("temperaments.json 必须是非空对象")
        if not isinstance(assignments, dict):
            raise ValueError("temperament_config.json 必须是对象")
        return temperaments, assignments

    def commit_candidate(self, candidate: tuple[dict, dict]) -> None:
        self.temperaments, self.temperament_dict = candidate

    def get_temperament(self, qq=None) -> str:
        """根据qq获取每个群友的性格配置"""
        from .runtime_snapshot import runtime_snapshots

        snapshot = runtime_snapshots.active()
        assignments = (
            snapshot.temperament_assignments
            if snapshot is not None
            else self.temperament_dict
        )
        if qq:
            qq = str(qq)
            return assignments.get(qq, "默认")
        return "默认"

    def get_temperaments_keys(self) -> list:
        from .runtime_snapshot import runtime_snapshots

        snapshot = runtime_snapshots.active()
        return (
            snapshot.temperaments.keys()
            if snapshot is not None
            else self.temperaments.keys()
        )

    def get_all_temperaments(self) -> str:
        from .runtime_snapshot import runtime_snapshots

        snapshot = runtime_snapshots.active()
        temperaments = (
            snapshot.temperaments if snapshot is not None else self.temperaments
        )
        return json.dumps(dict(temperaments), indent=4, ensure_ascii=False)

    def get_temperament_prompt(self, temperament: str) -> str:
        """根据性格获取提示词"""
        from .runtime_snapshot import runtime_snapshots

        snapshot = runtime_snapshots.active()
        temperaments = (
            snapshot.temperaments if snapshot is not None else self.temperaments
        )
        return temperaments.get(temperament, "你是ai助手。回答像真人且简短")

    def set_temperament_dict(self, qq, temperament) -> bool:
        """设置配置项的值"""
        qq = str(qq)
        self.temperament_dict[qq] = temperament
        written = self.write_temperament(qq, temperament)
        if written:
            from .runtime_snapshot import immutable_mapping, runtime_snapshots

            runtime_snapshots.patch_current(
                temperament_assignments=immutable_mapping(self.temperament_dict)
            )
        return written

    # 读取文件
    def read_temperament(self) -> dict:
        if not self.temperament_config.exists():
            self.temperament_config.parent.mkdir(parents=True, exist_ok=True)
            self.temperament_config.touch()
            with open(self.temperament_config, "w", encoding="utf-8") as f:
                json.dump({}, f, ensure_ascii=False, indent=4)
            return {}
        try:
            with open(self.temperament_config, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            logger.error(format_exc())
            return {}

    # 读取文件
    def read_temperaments(self) -> dict:
        prompt = "你是ai助手。回答像真人且简短"
        default_temperaments = {"默认": prompt}
        if not self.temperaments_path.exists():
            self.temperaments_path.parent.mkdir(parents=True, exist_ok=True)
            self.temperaments_path.touch()
            with open(self.temperaments_path, "w", encoding="utf-8") as f:
                json.dump(default_temperaments, f, ensure_ascii=False, indent=4)
            return default_temperaments
        try:
            with open(self.temperaments_path, encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    return data
        except Exception:
            logger.error(format_exc())
        return default_temperaments

    # 性格写入文件
    def write_temperament(self, qq: int, temperament: str) -> bool:
        if not self.temperament_config.exists():
            self.temperament_config.parent.mkdir(parents=True, exist_ok=True)

            self.temperament_config.touch()
        try:
            with open(self.temperament_config, "r+", encoding="utf-8") as f:
                if data := f.read():
                    dict_ = json.loads(data)
                    dict_[qq] = temperament
                else:
                    dict_ = {qq: temperament}
                f.seek(0)
                json.dump(dict_, f, ensure_ascii=False, indent=4)
                f.truncate()
                return True
        except Exception:
            logger.error(format_exc())
            return False


temperament_manager = TemperamentManager()
