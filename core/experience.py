"""core/experience.py — 经验固化与回放调度（阶段2·规划版）。

打通"执行记录 → 可复用技能"闭环（规划 4.3）：
- 技能提取：从成功轨迹提炼可复用脚本/配置/提示词模板，存入技能库。
- 回放优先调度：新任务先检索历史成功路径，命中则回放复用，减少重复推理。
- 复用统计：记录技能被调用/复用次数，供评估使用。
"""
from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class Skill:
    """可复用技能：由成功轨迹提炼。"""

    id: str
    name: str
    signature: str                # 任务签名（检索键）
    steps: List[Dict[str, Any]]   # 提炼后的步骤模板
    prompt_template: str = ""     # 提示词模板
    source_fork_id: str = ""      # 来源分支
    source_trajectory: str = ""   # 来源轨迹摘要
    created_at: float = field(default_factory=time.time)
    reuse_count: int = 0          # 复用次数（评估指标）
    success_count: int = 0        # 回放成功次数

    def to_dict(self) -> dict:
        return self.__dict__.copy()

    @classmethod
    def from_dict(cls, d: dict) -> "Skill":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


class SkillLibrary:
    """技能库：技能的存储、检索、复用统计。"""

    def __init__(self, store_dir: str):
        self.store_dir = store_dir
        os.makedirs(store_dir, exist_ok=True)
        self._path = os.path.join(store_dir, "skills.json")
        self._skills: Dict[str, Skill] = {}
        self._load()

    def _load(self) -> None:
        if os.path.exists(self._path):
            with open(self._path, encoding="utf-8") as f:
                data = json.load(f)
            self._skills = {s["id"]: Skill.from_dict(s) for s in data.get("skills", [])}

    def _save(self) -> None:
        tmp = self._path + f".tmp-{uuid.uuid4().hex}"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"skills": [s.to_dict() for s in self._skills.values()]},
                      f, ensure_ascii=False)
        os.replace(tmp, self._path)

    # ------------------------------------------------------------------
    def add(self, skill: Skill) -> Skill:
        self._skills[skill.id] = skill
        self._save()
        return skill

    def get(self, skill_id: str) -> Optional[Skill]:
        return self._skills.get(skill_id)

    def list(self) -> List[Skill]:
        return sorted(self._skills.values(), key=lambda s: s.created_at)

    def search(self, signature: str, top_k: int = 3) -> List[Skill]:
        """按任务签名模糊检索技能（包含匹配 + 词汇重合度排序）。"""
        def score(s: Skill) -> float:
            if s.signature == signature:
                return 100.0
            a, b = set(signature.lower().split()), set(s.signature.lower().split())
            if not a or not b:
                return 0.0
            return len(a & b) / len(a | b)
        ranked = sorted((s for s in self._skills.values() if score(s) > 0),
                        key=lambda s: (score(s), s.reuse_count), reverse=True)
        return ranked[:top_k]

    def record_reuse(self, skill_id: str, success: bool = True) -> None:
        """记录一次复用（评估指标）。"""
        s = self._skills.get(skill_id)
        if s:
            s.reuse_count += 1
            if success:
                s.success_count += 1
            self._save()

    def stats(self) -> dict:
        skills = list(self._skills.values())
        total_reuse = sum(s.reuse_count for s in skills)
        total_success = sum(s.success_count for s in skills)
        return {
            "skill_count": len(skills),
            "total_reuse": total_reuse,
            "total_success": total_success,
            "reuse_rate": round(total_success / total_reuse, 3) if total_reuse else 0.0,
        }


class ExperienceEngine:
    """经验固化引擎：轨迹 → 技能提取 → 检索 → 回放优先调度。"""

    def __init__(self, skill_library: SkillLibrary):
        self.library = skill_library

    # ------------------------------------------------------------------
    # 技能提取
    # ------------------------------------------------------------------
    def extract_skill(self, signature: str, steps: List[Dict[str, Any]],
                      name: str = "", source_fork_id: str = "") -> Skill:
        """从成功轨迹提炼技能：压缩为步骤模板 + 提示词模板。"""
        # 步骤压缩：仅保留 tool_call、关键参数与 env_change
        template_steps = [
            {"tool_call": s.get("tool_call", ""),
             "tool_params": s.get("tool_params", {}),
             "env_change": s.get("env_change", "")}
            for s in steps if s.get("tool_call")
        ]
        # 提示词模板
        lines = [f"任务：{signature}", "参考执行路径："]
        for i, s in enumerate(template_steps, 1):
            params = ", ".join(f"{k}={v!r}" for k, v in s["tool_params"].items())
            lines.append(f"  {i}. {s['tool_call']}({params}) → {s['env_change']}")
        skill = Skill(
            id=uuid.uuid4().hex,
            name=name or f"skill-{signature[:20]}",
            signature=signature,
            steps=template_steps,
            prompt_template="\n".join(lines),
            source_fork_id=source_fork_id,
            source_trajectory=f"{len(steps)} steps",
        )
        return self.library.add(skill)

    # ------------------------------------------------------------------
    # 回放优先调度
    # ------------------------------------------------------------------
    def plan_from_experience(self, signature: str) -> Optional[Dict[str, Any]]:
        """回放优先调度：新任务先查技能库，命中返回回放计划。

        返回 None 表示无历史路径，需要正常推理。
        命中时技能 reuse_count 自动 +1。
        """
        hits = self.library.search(signature, top_k=1)
        if not hits:
            return None
        skill = hits[0]
        self.library.record_reuse(skill.id, success=False)  # 先记调用，回放成功后再记成功
        return {
            "skill_id": skill.id,
            "skill_name": skill.name,
            "steps": skill.steps,
            "prompt_template": skill.prompt_template,
            "replay": True,
        }

    def confirm_replay_success(self, skill_id: str) -> None:
        """回放成功确认：success_count +1（复用率分子）。"""
        self.library.record_reuse(skill_id, success=True)
