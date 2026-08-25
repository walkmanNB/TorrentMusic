#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
lrcrequest.py —— 去中心化多源歌词获取模块（基于 lrclib.net 开放 API）

依赖安装（在项目根目录执行）：
    pip install requests opencc-python-reimplemented
    * opencc 未安装时自动优雅降级：仅用原始关键词搜索，不影响运行

核心能力：
  1) 简繁双轨搜索：对「标题 + 歌手」用 OpenCC 生成 简体/繁体 变体，
     多路并发请求 https://lrclib.net/api/search ，按 id 去重合并候选池，
     确保不漏掉任何异体字版本；
  2) 智能模糊匹配：本地轻量评分 —— NFKC 归一化去杂质 + 序列相似度
     (difflib) + 关键词重合度(Dice) + 包含关系加分 + 时长接近度加成，
     自动从候选中挑选"最像"的一条，绝不要求 100% 一致；
  3) 歌词解析与转换：优先 syncedLyrics（带时间轴），降级 plainLyrics，
     全文统一转换为简体中文。

本模块不依赖 PyQt5，可独立复用；所有网络操作均为阻塞式，
调用方必须自行放入后台线程（如 threading.Thread），严禁在 UI 线程直调。
"""

import difflib
import re
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import List, Optional, Set, Tuple

import requests

# ======================================================================
# OpenCC 可选依赖（优雅降级）
# ======================================================================
try:
    from opencc import OpenCC as _OpenCC
    _CC_T2S = _OpenCC("t2s")          # 繁体 -> 简体
    _CC_S2T = _OpenCC("s2t")          # 简体 -> 繁体
    OPENCC_OK = True
except Exception as _e:               # ImportError 或字典缺失等
    _CC_T2S = _CC_S2T = None
    OPENCC_OK = False
    print("[Lrc] opencc 未安装，简繁双轨搜索已降级为仅原始关键词 "
          "(pip install opencc-python-reimplemented 可启用):", _e)


def to_simplified(text: str) -> str:
    """统一转简体；opencc 缺失时原样返回。"""
    if not text or not OPENCC_OK:
        return text or ""
    try:
        return _CC_T2S.convert(text)
    except Exception:
        return text


def to_traditional(text: str) -> str:
    """转繁体（用于生成搜索变体）；opencc 缺失时原样返回。"""
    if not text or not OPENCC_OK:
        return text or ""
    try:
        return _CC_S2T.convert(text)
    except Exception:
        return text


# ======================================================================
# 归一化 / 分词 / 模糊匹配评分（纯标准库，轻量高效）
# ======================================================================
_BRACKETS_RE = re.compile(
    r"[（(【\[\{｛〔<《〈][^）)】\]\}〕>》〉]*[）)】\]\}〕>》〉]")   # 去 （Live)/(Remix)/【伴奏】 等
_PUNCT_RE = re.compile(r"[^\w\u4e00-\u9fff\u3040-\u30ff]+", re.UNICODE)
_CJK_RE = re.compile(r"[\u4e00-\u9fff\u3040-\u30ff]")


def normalize(text: str) -> str:
    """匹配用归一化：NFKC 全半角折叠 → 去括号杂质 → 去标点空白 → 小写。"""
    if not text:
        return ""
    t = unicodedata.normalize("NFKC", str(text))
    t = _BRACKETS_RE.sub("", t)
    t = _PUNCT_RE.sub("", t)
    return t.lower()


def tokenize(text: str) -> Set[str]:
    """轻量分词：连续拉丁/数字串为一个词，CJK 单字各算一个。"""
    t = normalize(text)
    if not t:
        return set()
    tokens: Set[str] = set()
    buf = ""
    for ch in t:
        if _CJK_RE.match(ch):
            if buf:
                tokens.add(buf)
                buf = ""
            tokens.add(ch)
        else:
            buf += ch
    if buf:
        tokens.add(buf)
    return tokens


def seq_ratio(a: str, b: str) -> float:
    """归一化后的序列相似度（0~1）。"""
    na, nb = normalize(a), normalize(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    return difflib.SequenceMatcher(None, na, nb).ratio()


def dice_overlap(a: str, b: str) -> float:
    """关键词重合度：Dice 系数 = 2|A∩B| / (|A|+|B|)。"""
    ta, tb = tokenize(a), tokenize(b)
    if not ta or not tb:
        return 0.0
    return 2.0 * len(ta & tb) / (len(ta) + len(tb))


def containment(a: str, b: str) -> float:
    """包含关系加分：短串完整出现在长串中（候选带杂质时极有用）。"""
    na, nb = normalize(a), normalize(b)
    if not na or not nb:
        return 0.0
    short, long_ = (na, nb) if len(na) <= len(nb) else (nb, na)
    if short == long_:
        return 1.0
    if short in long_:
        # 被包含的串越长越可信，避免单字误命中
        return min(1.0, 0.6 + 0.4 * len(short) / max(len(long_), 1))
    return 0.0


def field_score(query: str, cand: str) -> float:
    """单字段（标题或歌手）综合得分：相似度 + 重合度 + 包含关系。"""
    return (0.55 * seq_ratio(query, cand)
            + 0.25 * dice_overlap(query, cand)
            + 0.20 * containment(query, cand))


def match_score(q_title: str, q_artist: str,
                c_title: str, c_artist: str,
                q_duration_s: Optional[float] = None,
                c_duration_s: Optional[float] = None) -> Tuple[float, float, float]:
    """总评分：标题为主、歌手为辅，时长接近度微调。返回 (总分, 标题分, 歌手分)。"""
    ts = field_score(q_title, c_title)
    as_ = field_score(q_artist, c_artist)
    total = 0.68 * ts + 0.32 * as_
    if q_duration_s and c_duration_s:
        diff = abs(float(q_duration_s) - float(c_duration_s))
        if diff <= 2:
            total += 0.08
        elif diff <= 5:
            total += 0.04
        elif diff > 20:
            total -= 0.05
    return max(0.0, min(1.0, total)), ts, as_


# ======================================================================
# LRC 解析与简体统一
# ======================================================================
_TIME_RE = re.compile(r"\[(\d{1,3}):(\d{1,2})(?:[.:](\d{1,3}))?\]")
_META_TAG_RE = re.compile(r"^\s*\[[a-zA-Z]")


@dataclass
class LyricLine:
    time_ms: int          # -1 表示无时间轴（纯文本模式）
    text: str


def parse_lrc(raw: str) -> List[LyricLine]:
    """解析 LRC：支持一行多时间戳 [mm:ss.xx][mm:ss]歌词、忽略 [ti:] 等元数据头。
    输出按时间升序。"""
    out: List[LyricLine] = []
    if not raw:
        return out
    for raw_line in raw.splitlines():
        if not raw_line.strip():
            continue
        matches = list(_TIME_RE.finditer(raw_line))
        if not matches:
            txt = raw_line.strip()
            if txt and not _META_TAG_RE.match(raw_line):
                out.append(LyricLine(-1, txt))
            continue
        for i, m in enumerate(matches):
            end = matches[i + 1].start() if i + 1 < len(matches) else len(raw_line)
            seg = raw_line[m.end():end].strip()
            if not seg:
                continue
            t = int(m.group(1)) * 60_000 + int(m.group(2)) * 1000
            frac = m.group(3)
            if frac:
                t += int((frac + "000")[:3])
            out.append(LyricLine(t, seg))
    out.sort(key=lambda l: l.time_ms)
    return out


def plain_to_lines(raw: str) -> List[LyricLine]:
    """纯文本歌词 → 无时间轴行列表。"""
    return [LyricLine(-1, s.strip())
            for s in (raw or "").splitlines() if s.strip()]


def simplify_lines(lines: List[LyricLine]) -> List[LyricLine]:
    """全文统一转换为简体中文（就地修改并返回）。"""
    for ln in lines:
        ln.text = to_simplified(ln.text)
    return lines


# ======================================================================
# LrclibProvider：搜索插件主体
# ======================================================================
@dataclass
class LyricCandidate:
    id: int = 0
    track_name: str = ""
    artist_name: str = ""
    album_name: str = ""
    duration: float = 0.0
    instrumental: bool = False
    synced_raw: str = ""
    plain_raw: str = ""


class LrclibProvider:
    """lrclib.net 歌词提供器：双轨并发搜索 → 去重合并 → 模糊选优 → 解析转简。"""

    SEARCH_URL = "https://lrclib.net/api/search"
    TIMEOUT = 8                                   # 单请求超时（秒）
    UA = {"User-Agent": "MiniGlassPlayer/1.0 (lyrics fetcher)"}
    MAX_QUERIES = 4                               # 变体组合上限，防请求风暴
    MIN_SCORE = 0.35                              # 低于此分视为"没有像的"

    # ---------- 简繁双轨变体 ----------
    @classmethod
    def variant_pairs(cls, title: str, artist: str) -> List[Tuple[str, str]]:
        """生成 (标题, 歌手) 查询组合：原始 / 双简 / 双繁 / 繁题简家，去重截断。"""
        pairs: List[Tuple[str, str]] = []
        seen: Set[Tuple[str, str]] = set()

        def add(t: str, a: str):
            key = (normalize(t), normalize(a))
            if not key[0] or key in seen:
                return
            seen.add(key)
            pairs.append((t.strip(), a.strip()))

        add(title, artist)
        add(to_simplified(title), to_simplified(artist))     # 订阅源是繁体时
        add(to_traditional(title), to_traditional(artist))   # 订阅源是简体而库是繁体时
        add(title, to_simplified(artist))                    # 题家异体错配兜底
        return pairs[: cls.MAX_QUERIES]

    # ---------- 单路请求 ----------
    @classmethod
    def _search_once(cls, title: str, artist: str) -> list:
        try:
            r = requests.get(
                cls.SEARCH_URL,
                params={"track_name": title, "artist_name": artist},
                headers=cls.UA,
                timeout=cls.TIMEOUT,
            )
            r.raise_for_status()
            data = r.json()
            return data if isinstance(data, list) else []
        except Exception as e:
            print(f"[Lrc] 搜索失败 {title!r}/{artist!r}: {e}")
            return []

    # ---------- 多路并发 + 去重合并 ----------
    @classmethod
    def search_pool(cls, title: str, artist: str) -> List[LyricCandidate]:
        pairs = cls.variant_pairs(title, artist)
        pool: dict = {}
        if not pairs:
            return []
        workers = min(len(pairs), 4)
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = [ex.submit(cls._search_once, t, a) for t, a in pairs]
            for fut in as_completed(futs):
                for item in fut.result():
                    if not isinstance(item, dict):
                        continue
                    cid = item.get("id")
                    if cid is None or cid in pool:       # 按 id 去重合并
                        continue
                    try:
                        dur = float(item.get("duration") or 0)
                    except (TypeError, ValueError):
                        dur = 0.0
                    pool[cid] = LyricCandidate(
                        id=int(cid),
                        track_name=str(item.get("trackName") or ""),
                        artist_name=str(item.get("artistName") or ""),
                        album_name=str(item.get("albumName") or ""),
                        duration=dur,
                        instrumental=bool(item.get("instrumental")),
                        synced_raw=str(item.get("syncedLyrics") or ""),
                        plain_raw=str(item.get("plainLyrics") or ""),
                    )
        return list(pool.values())

    # ---------- 模糊选优 ----------
    @classmethod
    def pick_best(cls, pool: List[LyricCandidate], title: str, artist: str,
                  duration_s: Optional[float] = None
                  ) -> Tuple[Optional[LyricCandidate], float]:
        best, best_score, best_has_synced = None, -1.0, False
        for cand in pool:
            score, _, _ = match_score(
                title, artist, cand.track_name, cand.artist_name,
                duration_s, (cand.duration or None))
            
            has_synced = bool(cand.synced_raw and cand.synced_raw.strip())
            
            # 判断是否应该替换当前最优解：
            # 1. 如果之前没有最优解，直接选中
            # 2. 如果当前候选有时间轴，而之前最优解没有时间轴，只要当前候选分数不是极其低下（比如在合理范围内，例如分差小于 0.25 或者得分更高），则优先选择有时间轴的
            # 3. 如果两者的 synced 状态相同（都有或都没有），则按基础得分高低决定
            # 4. 如果当前候选没有时间轴，而之前最优解有时间轴，则只有当当前候选分数显著更高（例如高出 0.15 以上）时才替换
            should_replace = False
            if best is None:
                should_replace = True
            else:
                if has_synced and not best_has_synced:
                    if score >= best_score - 0.25:
                        should_replace = True
                elif not has_synced and best_has_synced:
                    if score > best_score + 0.15:
                        should_replace = True
                else:
                    if score > best_score:
                        should_replace = True
            
            if should_replace:
                best, best_score, best_has_synced = cand, score, has_synced

        if best is None or best_score < cls.MIN_SCORE:
            return None, max(best_score, 0.0)
        return best, best_score

    # ---------- 一站式入口 ----------
    @classmethod
    def fetch_best(cls, title: str, artist: str,
                   duration_ms: Optional[int] = None) -> dict:
        """搜索 → 合并 → 选优 → 解析 → 统一简体。
        返回 {"lines": [LyricLine...], "synced": bool, "score": float,
              "matched_title": str, "matched_artist": str, "reason": str}
        失败时 lines 为空并给出 reason。本方法阻塞，请放后台线程调用。"""
        duration_s = (duration_ms / 1000.0) if duration_ms else None
        pool = cls.search_pool(title, artist)
        if not pool:
            return {"lines": [], "synced": False, "score": 0.0,
                    "matched_title": "", "matched_artist": "", "reason": "曲库无候选结果"}
        best, score = cls.pick_best(pool, title, artist, duration_s)
        if best is None:
            return {"lines": [], "synced": False, "score": round(score, 3),
                    "matched_title": "", "matched_artist": "",
                    "reason": f"匹配度过低({score:.2f})"}
        # syncedLyrics 优先，plainLyrics 降级
        lines = parse_lrc(best.synced_raw) if best.synced_raw.strip() else []
        synced = bool(lines)
        if not lines:
            lines = plain_to_lines(best.plain_raw)
        if not lines and best.instrumental:
            lines = [LyricLine(-1, "♪ 纯音乐 · 无歌词 ♪")]
        simplify_lines(lines)                            # 全文统一转简体
        return {
            "lines": lines,
            "synced": synced,
            "score": round(score, 3),
            "matched_title": best.track_name,
            "matched_artist": best.artist_name,
            "reason": "" if lines else "该曲目暂无歌词文本",
        }


if __name__ == "__main__":     # 自测：python lrcrequest.py
    demo = parse_lrc("[ti:test]\n[00:01.00]Hello [00:05.5]World\n[00:08]再见")
    print([(l.time_ms, l.text) for l in demo])
    print("match:", match_score("晴天", "周杰伦", "晴天 (Live)", "周杰伦", 200, 201))
