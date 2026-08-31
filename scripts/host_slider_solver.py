#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
host_slider_solver.py - 闲鱼自动回复系统「宿主机过滑块」服务（方案A）

使用 DrissionPage（与容器内兜底引擎相同）在有头模式下自动完成滑块验证。
DrissionPage 的反检测能力远强于 Playwright，容器内无头模式即可通过，
宿主机有头模式通过率更高。

协议（与 common/services/captcha/orchestrator.py::_call_remote_solve 完全兼容）
----------------------------------------------------------------------------
请求  POST {url}   JSON 体:
    secret_key       约定密钥
    account_id       账号标识
    url              闲鱼滑块验证页面链接；为空表示连通性测试
    browser_timeout  浏览器执行超时秒数
    cookies          账号 Cookie 字符串（可选）
    device_id        设备 ID（可选）

响应  200 JSON:
    {"success": true,  "data": {"cookies": {name: value, ...}}}
    {"success": false, "message": "..."}
    {"success": false, "data": {"url_expired": true}, "message": "..."}

用法
----
    pip install DrissionPage
    python host_slider_solver.py --port 8787 --secret 你的秘钥
"""

import argparse
import json
import logging
import math
import os
import random
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional, Tuple

LOG = logging.getLogger("host_slider_solver")

MIN_SLIDER_TIMEOUT = 90
MAX_SLIDER_TIMEOUT = 240

_solve_lock = threading.Lock()


# ==================== 拟人化轨迹生成（移植自 drissionpage_tracks.py） ====================

def ease_out_expo(t: float) -> float:
    return 1 - pow(2, -10 * t) if t != 1 else 1


def generate_tracks(distance: float, target_points: Optional[int] = None) -> List[int]:
    """生成拟人化滑动轨迹（绝对位置列表）。"""
    tracks: List[float] = []
    current = 0.0
    velocity = 0.0
    max_velocity = random.uniform(80, 150)
    acceleration_phase = distance * random.uniform(0.3, 0.6)
    deceleration_start = distance * random.uniform(0.6, 0.85)

    if target_points:
        base_dt = distance / (target_points * max_velocity * 0.5)
        dt = base_dt * random.uniform(0.8, 1.2)
        dt = max(0.01, min(0.2, dt))
    else:
        dt = random.uniform(0.02, 0.12)

    hesitation_probability = 0.15
    overshoot_chance = 0.3
    tracks.append(0.0)
    step = 0
    hesitation_counter = 0

    while current < distance:
        step += 1
        if current < acceleration_phase:
            target_accel = random.uniform(15, 35)
            if step % random.randint(3, 8) == 0:
                target_accel *= random.uniform(0.7, 1.4)
        elif current < deceleration_start:
            target_accel = random.uniform(-2, 2)
            if random.random() < 0.2:
                target_accel = random.uniform(-8, 8)
        else:
            remaining_distance = distance - current
            if remaining_distance > 20:
                target_accel = random.uniform(-25, -8)
            else:
                target_accel = random.uniform(-15, -3)

        if random.random() < hesitation_probability and current > acceleration_phase:
            hesitation_counter += 1
            if hesitation_counter < 3:
                if random.random() < 0.4:
                    target_accel = random.uniform(-8, -2)
                else:
                    target_accel = random.uniform(-2, 2)
            else:
                hesitation_counter = 0

        velocity = velocity * 0.95 + target_accel * dt
        velocity = max(0.0, min(velocity, max_velocity))
        old_current = current
        current += velocity * dt

        if len(tracks) > 5:
            tremor = random.uniform(-0.3, 0.3) * (velocity / max_velocity)
            current += tremor

        if random.random() < 0.12 and current > 50:
            correction_type = random.random()
            if correction_type < 0.6:
                current -= random.uniform(1.0, 4.0)
            elif correction_type < 0.8:
                pass
            else:
                current += random.uniform(0.2, 1.0)

        if current < old_current:
            current = old_current + random.uniform(0.1, 0.8)
        if current - old_current > 15:
            current = old_current + random.uniform(8, 15)
        tracks.append(round(current, 1))

    if random.random() < overshoot_chance:
        overshoot = random.uniform(2, 8)
        tracks.append(round(distance + overshoot, 1))
        correction_steps = random.randint(2, 5)
        for i in range(correction_steps):
            correction = overshoot * (1 - (i + 1) / correction_steps)
            noise = random.uniform(-0.3, 0.3)
            tracks.append(round(distance + correction + noise, 1))

    final_adjustments = random.randint(1, 3)
    target_final = distance + random.uniform(-1, 2)
    for _ in range(final_adjustments):
        target_final += random.uniform(-0.5, 0.5)
        tracks.append(round(target_final, 1))

    cleaned = _clean_tracks(tracks)
    cleaned = _resample_tracks(cleaned, target_points)
    return [int(x) for x in cleaned]


def _clean_tracks(tracks: List[float]) -> List[float]:
    cleaned = [tracks[0]]
    last_pos = tracks[0]
    for i in range(1, len(tracks)):
        current_pos = tracks[i]
        if abs(current_pos - last_pos) < 1.5:
            continue
        if current_pos >= last_pos or (last_pos - current_pos) < 3:
            cleaned.append(current_pos)
            last_pos = current_pos
        else:
            corrected_pos = last_pos + random.uniform(0.1, 1.0)
            cleaned.append(corrected_pos)
            last_pos = corrected_pos
    return cleaned


def _resample_tracks(cleaned: List[float], target_points: Optional[int]) -> List[float]:
    if target_points and len(cleaned) != target_points and len(cleaned) > 1:
        if len(cleaned) > target_points:
            step = len(cleaned) / target_points
            optimized = [cleaned[0]]
            for i in range(1, target_points - 1):
                idx = min(int(i * step), len(cleaned) - 1)
                optimized.append(cleaned[idx])
            optimized.append(cleaned[-1])
            return optimized
        while len(cleaned) < target_points and len(cleaned) > 1:
            new_tracks = [cleaned[0]]
            for i in range(len(cleaned) - 1):
                new_tracks.append(cleaned[i])
                if len(new_tracks) < target_points:
                    mid_point = (cleaned[i] + cleaned[i + 1]) / 2 + random.uniform(-0.5, 0.5)
                    new_tracks.append(mid_point)
            new_tracks.append(cleaned[-1])
            cleaned = new_tracks
            if len(cleaned) >= target_points:
                cleaned = cleaned[:target_points]
                break
        return cleaned
    if not target_points and len(cleaned) > 200:
        step = max(1, len(cleaned) // 150)
        optimized = [cleaned[i] for i in range(0, len(cleaned), step)]
        if optimized[-1] != cleaned[-1]:
            optimized.append(cleaned[-1])
        return optimized
    return cleaned


# ==================== 滑块运动执行（移植自 drissionpage_motion.py） ====================

def calculate_slide_distance(page: Any, log_tag: str = "") -> int:
    """动态计算滑动距离。"""
    try:
        track_selectors = ["#nc_1__scale_text", ".nc-lang-cnt", "#nc_1_wrapper", ".nc_wrapper"]
        for selector in track_selectors:
            try:
                track_element = page.ele(selector, timeout=2)
                if track_element and track_element.rect and track_element.rect.width > 0:
                    track_width = track_element.rect.width
                    slide_ratio = random.uniform(0.70, 0.90)
                    final_distance = int(track_width * slide_ratio) + random.randint(-20, 20)
                    final_distance = max(200, min(600, final_distance))
                    LOG.info("[%s] 轨道宽度 %dpx -> 滑动距离 %dpx", log_tag, track_width, final_distance)
                    return final_distance
            except Exception:
                continue
    except Exception as e:
        LOG.warning("[%s] 轨道宽度计算失败: %s", log_tag, e)
    try:
        page_width = page.size[0] if hasattr(page, "size") else 1920
    except Exception:
        page_width = 1920
    if page_width <= 1366:
        return random.randint(250, 320)
    if page_width <= 1920:
        return random.randint(300, 400)
    return random.randint(350, 480)


def execute_tracks(page: Any, tracks: List[int], target_total_time: float, log_tag: str = "") -> None:
    """按轨迹逐步移动鼠标，叠加垂直抖动与 sin 节律速度波动。"""
    if not tracks:
        return
    start_time = time.time()
    slide_direction = random.choice([-1, 1])
    y_drift_trend = random.uniform(-3, 3)
    total = len(tracks)

    for i in range(total):
        progress = i / total
        offset_x = tracks[i] if i == 0 else tracks[i] - tracks[i - 1]
        if abs(offset_x) < 0.1:
            continue

        trend_offset = y_drift_trend * (progress ** 0.7)
        shake_offset = random.uniform(-1.5, 1.5)
        if abs(offset_x) > 8:
            shake_offset *= random.uniform(1.2, 1.8)
        directional_offset = slide_direction * random.uniform(0.2, 1.0)
        offset_y = max(-8, min(8, trend_offset + shake_offset + directional_offset))

        elapsed = time.time() - start_time
        remaining_time = max(target_total_time - elapsed, 0.1)
        remaining_steps = total - i
        base_time_per_step = remaining_time / remaining_steps if remaining_steps > 0 else 0.01
        distance_factor = max(abs(offset_x) / 15.0, 0.3)
        base_duration = base_time_per_step * distance_factor * 0.7

        rhythm_factor = 1 + 0.3 * math.sin(i * 0.5) * random.uniform(0.5, 1.5)
        if progress < 0.2:
            phase_multiplier = random.uniform(1.5, 2.5)
        elif progress < 0.7:
            phase_multiplier = random.uniform(0.3, 1.0)
        else:
            phase_multiplier = random.uniform(1.5, 3.0)
        final_duration = base_duration * phase_multiplier * rhythm_factor * random.uniform(0.7, 1.3)
        final_duration = max(0.005, min(0.15, final_duration))

        try:
            page.actions.move(
                offset_x=int(offset_x),
                offset_y=int(offset_y),
                duration=max(0.005, float(final_duration)),
            )
        except Exception:
            continue
        step_delay = max(0.001, min(0.05, base_time_per_step * 0.3 * random.uniform(0.5, 1.5)))
        time.sleep(step_delay)


# ==================== Cookie 工具 ====================

def parse_cookie_string(raw: str) -> Dict[str, str]:
    result: Dict[str, str] = {}
    for part in (raw or "").split(";"):
        part = part.strip()
        if "=" not in part:
            continue
        name, value = part.split("=", 1)
        name = name.strip()
        if name:
            result[name] = value.strip()
    return result


CAPTCHA_NOT_REQUIRED = "__NOT_REQUIRED__"


def _refresh_captcha_url(cookies_str: str, device_id: str, account_id: str) -> Optional[str]:
    """用 cookies 重取滑块验证链接（与容器内 url_provider 逻辑一致）。

    调用闲鱼 h5api 获取新的验证 URL；如果返回不需要验证则返回 CAPTCHA_NOT_REQUIRED。
    """
    try:
        import requests as _req
        cookies_dict = parse_cookie_string(cookies_str)
        # 闲鱼 token 刷新接口
        api_url = "https://h5api.m.goofish.com/h5/mtop.taobao.idle.web.token.refresh/1.0/"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
            "Content-Type": "application/x-www-form-urlencoded",
        }
        resp = _req.post(api_url, data={
            "deviceId": device_id,
        }, cookies=cookies_dict, headers=headers, timeout=10)
        data = resp.json()
        # 检查返回中是否有验证 URL
        if isinstance(data, dict):
            ret = data.get("data", {}).get("ret", [])
            if isinstance(ret, list) and ret:
                url_val = ret[0].get("url", "")
                if url_val:
                    return url_val
            # 如果返回不需要验证
            if data.get("data", {}).get("needCaptcha") is False:
                return CAPTCHA_NOT_REQUIRED
        return None
    except Exception as e:
        LOG.warning("[account=%s] 重取验证链接失败: %s", account_id, e)
        return None


def _is_x5sec_cookie(name: str) -> bool:
    lowered = name.lower()
    return lowered.startswith("x5") or "x5sec" in lowered


def _has_fresh_x5sec(cookies: Dict[str, str], injected_x5: Dict[str, str]) -> bool:
    for name, value in cookies.items():
        if _is_x5sec_cookie(name):
            if name not in injected_x5:
                return True
            if injected_x5.get(name) != value:
                return True
    return False


# ==================== DrissionPage 滑块验证核心 ====================

SLIDER_SELECTOR = "#nc_1_n1z"
SLIDER_LOADED_SELECTOR = "x://span[contains(@id,'nc_1_n1z')]"
BLOCKED_TITLE = "验证码拦截"


def solve_slider(task: Dict[str, Any], forced_timeout: int) -> Tuple[int, Dict[str, Any]]:
    url = (task.get("url") or "").strip()
    account_id = (task.get("account_id") or "").strip() or "-"
    browser_timeout = 0
    try:
        browser_timeout = max(0, int(task.get("browser_timeout") or 0))
    except (TypeError, ValueError):
        browser_timeout = 0
    cookies_str = (task.get("cookies") or "").strip()
    device_id = (task.get("device_id") or "").strip()

    injected_x5: Dict[str, str] = {}
    for name, value in parse_cookie_string(cookies_str).items():
        if _is_x5sec_cookie(name):
            injected_x5[name] = value

    if not url:
        LOG.info("[account=%s] 连通性测试，返回 ok", account_id)
        return 200, {"success": True, "message": "ok", "data": {"cookies": {}}}

    if not _solve_lock.acquire(blocking=False):
        LOG.warning("[account=%s] 已有滑块在处理中，返回 503", account_id)
        return 503, {"success": False, "message": "已有滑块验证窗口在处理中，请稍后重试"}

    started = time.time()
    try:
        if forced_timeout and forced_timeout > 0:
            slider_timeout = forced_timeout
        else:
            slider_timeout = max(MIN_SLIDER_TIMEOUT, browser_timeout * 5 + 60)
        slider_timeout = min(slider_timeout, MAX_SLIDER_TIMEOUT)

        try:
            from DrissionPage import Chromium, ChromiumOptions
        except ImportError:
            LOG.error("DrissionPage 未安装，请先执行: pip install DrissionPage")
            return 500, {"success": False, "message": "宿主机未安装 DrissionPage，请执行 pip install DrissionPage"}

        # 持久化用户数据目录（与容器内兜底引擎一致，复用 browser_data/user_{account_id}）
        profile_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                   "browser_data", f"user_{account_id}")
        os.makedirs(profile_dir, exist_ok=True)

        # 清理残留 Singleton 锁文件（与容器内 _clean_singleton_lock_files 一致）
        for _fname in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
            _fpath = os.path.join(profile_dir, _fname)
            if os.path.exists(_fpath) or os.path.islink(_fpath):
                try:
                    if os.path.islink(_fpath):
                        os.unlink(_fpath)
                    else:
                        os.remove(_fpath)
                except Exception:
                    pass

        co = ChromiumOptions()
        # 优先使用 Playwright 的 Chromium（与容器内兜底引擎完全相同的二进制）
        # 其次系统 Chrome，最后 Edge
        import glob as _glob
        browser_candidates = []

        # Playwright Chromium（容器内同款）— 按版本号降序，优先最新
        pw_chromium_dirs = sorted(_glob.glob(os.path.join(
            os.environ.get("LOCALAPPDATA", ""),
            "ms-playwright", "chromium-*", "chrome-win64", "chrome.exe"
        )), reverse=True)
        browser_candidates.extend(pw_chromium_dirs)

        # 系统 Chrome
        browser_candidates.extend([
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        ])
        # 最后才用 Edge
        browser_candidates.extend([
            r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
            r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        ])

        browser_path = ""
        for p_ in browser_candidates:
            if os.path.isfile(p_):
                browser_path = p_
                break
        if browser_path:
            co.set_browser_path(browser_path)
            LOG.info("[account=%s] 使用浏览器: %s", account_id, browser_path)

        co.set_user_data_path(profile_dir)
        co.set_argument("--remote-debugging-port=0")
        co.headless(on_off=False)  # 有头模式，窗口可见，供人工拖滑块
        co.no_imgs(True)

        # 与容器内兜底引擎完全一致的反检测参数
        for arg in (
            "--no-sandbox",
            "--disable-setuid-sandbox",
            "--disable-dev-shm-usage",
            "--disable-gpu",
            "--disable-web-security",
            "--disable-features=VizDisplayCompositor",
            "--disable-blink-features=AutomationControlled",
            "--disable-extensions",
            "--no-first-run",
            "--disable-default-apps",
            "--disable-background-timer-throttling",
            "--disable-renderer-backgrounding",
            "--disable-backgrounding-occluded-windows",
            "--disable-ipc-flooding-protection",
            "--window-size=1920,1080",
        ):
            co.set_argument(arg)

        LOG.info("[account=%s] 启动浏览器（人工模式，超时=%ds），请手动拖动滑块", account_id, slider_timeout)
        browser = Chromium(co)
        page = browser.latest_tab

        try:
            # 注入 cookies
            if cookies_str:
                cookies_dict = parse_cookie_string(cookies_str)
                for name, value in cookies_dict.items():
                    try:
                        page.set.cookies({
                            "name": name,
                            "value": value,
                            "domain": ".goofish.com",
                            "path": "/",
                        })
                    except Exception:
                        pass
                LOG.info("[account=%s] 已注入 %d 个 cookie", account_id, len(cookies_dict))

            LOG.info("[account=%s] 打开验证页面，请手动拖动滑块", account_id)
            page.get(url)
            time.sleep(2)

            # 人工模式：不自动滑动，轮询等待用户手动完成
            check_interval = 2  # 每2秒检查一次
            elapsed = 0
            last_title = ""
            while time.time() - started < slider_timeout:
                try:
                    title = page.title
                except Exception:
                    title = ""

                # 检查是否已通过（标题变化 + x5sec cookie）
                if title != BLOCKED_TITLE:
                    cookies = {}
                    try:
                        for c in page.cookies():
                            if isinstance(c, dict):
                                cookies[c.get("name", "")] = str(c.get("value", ""))
                    except Exception:
                        pass
                    if _has_fresh_x5sec(cookies, injected_x5):
                        elapsed = int(time.time() - started)
                        LOG.info("[account=%s] 人工验证成功（%ds）", account_id, elapsed)
                        return 200, {"success": True, "message": "验证通过", "data": {"cookies": cookies}}
                    # 标题已变但还没有 x5sec，可能正在跳转
                    if title != last_title:
                        LOG.info("[account=%s] 页面标题变化: %s", account_id, title)
                        last_title = title

                # 检测链接过期页面
                if "页面访问出现了问题" in title or "访问出错" in title:
                    LOG.warning("[account=%s] 检测到链接过期页面", account_id)
                    # 如果有 cookies + device_id，尝试重新获取链接
                    if cookies_str and device_id:
                        LOG.info("[account=%s] 尝试用 cookies 重取验证链接", account_id)
                        new_url = _refresh_captcha_url(cookies_str, device_id, account_id)
                        if new_url and new_url != CAPTCHA_NOT_REQUIRED:
                            LOG.info("[account=%s] 已获取新链接，重新打开", account_id)
                            page.get(new_url)
                            time.sleep(2)
                            continue
                    return 200, {"success": False, "message": "验证链接已过期(url_expired)", "data": {"url_expired": True}}

                time.sleep(check_interval)
                elapsed = int(time.time() - started)
                if elapsed % 10 == 0:  # 每10秒输出一次等待日志
                    LOG.info("[account=%s] 等待人工验证（已等待%ds，标题: %s）", account_id, elapsed, title)

            LOG.warning("[account=%s] 人工验证超时（%ds）", account_id, int(time.time() - started))
            return 200, {"success": False, "message": "滑块验证超时，请重试"}
        finally:
            try:
                browser.quit()
            except Exception:
                pass
            # 持久化目录不删除（与容器一致）
    finally:
        _solve_lock.release()


# ==================== HTTP 服务 ====================

class SliderHandler(BaseHTTPRequestHandler):
    _secret = ""
    _forced_timeout = 0

    def log_message(self, fmt: str, *args: Any) -> None:
        LOG.info("[http] " + fmt % args)

    def _send_json(self, status: int, body: Dict[str, Any]) -> None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        if self.path.split("?")[0] == "/health":
            self._send_json(200, {"success": True, "message": "alive", "data": {"cookies": {}}})
        else:
            self._send_json(404, {"success": False, "message": "not found"})

    def do_POST(self) -> None:
        try:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            task = json.loads(raw.decode("utf-8")) if raw else {}
        except Exception as exc:
            self._send_json(400, {"success": False, "message": f"请求体解析失败: {exc}"})
            return
        if not isinstance(task, dict):
            self._send_json(400, {"success": False, "message": "请求体必须为 JSON 对象"})
            return
        secret = str(task.get("secret_key") or "").strip()
        if not secret:
            self._send_json(400, {"success": False, "message": "缺少秘钥"})
            return
        if not self._secret or secret != self._secret:
            self._send_json(400, {"success": False, "message": "秘钥无效"})
            return
        status, body = solve_slider(task, self._forced_timeout)
        self._send_json(status, body)


def main() -> int:
    parser = argparse.ArgumentParser(description="闲鱼自动回复-宿主机过滑块服务（DrissionPage 引擎）")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--secret", default=os.environ.get("SLIDER_SOLVER_SECRET", ""), help="秘钥")
    parser.add_argument("--timeout", type=int, default=0, help="单次验证超时秒数（0=自动）")
    parser.add_argument("--log-file", default="")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[logging.FileHandler(args.log_file, encoding="utf-8")] if args.log_file else [logging.StreamHandler(sys.stdout)],
    )
    if not args.secret:
        parser.error("必须提供 --secret")

    SliderHandler._secret = args.secret
    SliderHandler._forced_timeout = args.timeout

    server = ThreadingHTTPServer((args.host, args.port), SliderHandler)
    LOG.info("=" * 70)
    LOG.info("宿主机过滑块服务已启动（DrissionPage 引擎）")
    LOG.info("  监听: http://%s:%d", args.host, args.port)
    LOG.info("  引擎: 人工模式（弹出浏览器窗口，用户手动拖滑块）")
    LOG.info("  健康检查: GET /health")
    LOG.info("  容器配置 URL: http://host.docker.internal:%d", args.port)
    LOG.info("=" * 70)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        LOG.info("停止服务")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
