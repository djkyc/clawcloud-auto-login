#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ClawCloud 多账号自动保活脚本 - Selenium 版本
适配青龙面板 ARM Docker 环境
支持多账号、Cookie复用、2FA自动验证、Telegram/微信 通知--也支持国内的docker环境和软件路由
"""

import os
import sys
import time
import json
import requests
import re
import pyotp
from datetime import datetime
from loguru import logger
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, NoSuchElementException

# ============ 配置区域 ============

# 方式1: 直接在脚本中配置(不推荐,仅用于测试)
ACCOUNTS_CONFIG = []

# 方式2: 从环境变量读取(推荐)
# 在青龙面板中配置:
# CLAW_ACCOUNTS=账号1@example.com----密码1----2FA密钥1&账号2@example.com----密码2----2FA密钥2
# 格式: 用户名----密码----2FA密钥(可选)
# 多个账号用 & 分隔

def load_accounts_from_env():
    """从环境变量加载账号配置"""
    accounts = []
    
    # 优先使用环境变量
    env_accounts = os.environ.get("CLAW_ACCOUNTS", "").strip()
    
    if env_accounts:
        logger.info("从环境变量 CLAW_ACCOUNTS 加载账号配置")
        for acc_str in env_accounts.split("&"):
            parts = acc_str.strip().split("----")
            if len(parts) >= 2:
                account = {
                    "username": parts[0].split('#')[0].strip(),
                    "password": parts[1].strip(),
                    "totp_secret": parts[2].strip() if len(parts) > 2 else ""
                }
                accounts.append(account)
                logger.info(f"加载账号: {account['username']}")
    
    # 如果环境变量为空,使用脚本中的配置
    if not accounts and ACCOUNTS_CONFIG:
        logger.info("使用脚本中的账号配置")
        accounts = ACCOUNTS_CONFIG
    
    return accounts

ACCOUNTS = load_accounts_from_env()

TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN", "").strip()
TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "").strip()
WECHAT_API_URL = os.environ.get("WECHAT_API_URL", "").strip()
WECHAT_AUTH_TOKEN = os.environ.get("WECHAT_AUTH_TOKEN", "").strip()
CLAW_CLOUD_URL = os.environ.get("CLAW_CLOUD_URL", "https://eu-central-1.run.claw.cloud").strip()

# 脚本目录
SCRIPT_DIR = "/ql/data/scripts"

# ============ 代理配置 ============
# 优先读取 CLAW_PROXY, 其次是 ALL_PROXY, HTTP_PROXY
CLAW_PROXY = os.environ.get("CLAW_PROXY") or os.environ.get("ALL_PROXY") or os.environ.get("HTTP_PROXY")

if CLAW_PROXY:
    # 关键修复: 如果使用了代理，必须设置 no_proxy 排除 localhost
    # 否则 Selenium 无法连接 ChromeDriver
    os.environ["no_proxy"] = "localhost,127.0.0.1,::1"
    os.environ["NO_PROXY"] = "localhost,127.0.0.1,::1"
    
    # 确保 requests 库也能自动使用代理 (Telegram 需要)
    os.environ["http_proxy"] = CLAW_PROXY
    os.environ["https_proxy"] = CLAW_PROXY
    os.environ["HTTP_PROXY"] = CLAW_PROXY
    os.environ["HTTPS_PROXY"] = CLAW_PROXY
    
    logger.info(f"已启用代理配置: {CLAW_PROXY}")
    logger.info("已设置 NO_PROXY 环境变量以保护本地 WebDriver 连接")
else:
    logger.info("未检测到代理配置，使用直连模式")
# ================================


class Telegram:
    """Telegram 通知类"""
    
    def __init__(self):
        self.token = TG_BOT_TOKEN
        self.chat_id = int(TG_CHAT_ID) if TG_CHAT_ID and TG_CHAT_ID.isdigit() else None
        self.ok = bool(self.token and self.chat_id and self.token != "your_tg_bot_token")
        self.last_update_id = 0

    def send(self, msg):
        """发送 TG 消息"""
        if not self.ok:
            return
        try:
            requests.post(
                f"https://api.telegram.org/bot{self.token}/sendMessage",
                data={"chat_id": self.chat_id, "text": msg, "parse_mode": "HTML"},
                timeout=30
            )
        except Exception as e:
            logger.warning(f"TG 消息发送失败: {e}")

    def photo(self, path, caption=""):
        """发送 TG 图片"""
        if not self.ok or not os.path.exists(path):
            return None
        try:
            with open(path, 'rb') as f:
                resp = requests.post(
                    f"https://api.telegram.org/bot{self.token}/sendPhoto",
                    data={"chat_id": self.chat_id, "caption": caption[:1024]},
                    files={"photo": f},
                    timeout=60
                )
                if resp.ok:
                    return resp.json().get("result", {}).get("message_id")
        except Exception as e:
            logger.warning(f"TG 图片发送失败: {e}")
        return None

    def get_updates(self, offset=None, timeout=30):
        """获取 TG 更新"""
        if not self.ok:
            return []
        url = f"https://api.telegram.org/bot{self.token}/getUpdates"
        params = {"timeout": timeout}
        if offset:
            params["offset"] = offset
        try:
            resp = requests.get(url, params=params, timeout=timeout + 10)
            if resp.ok:
                return resp.json().get("result", [])
        except Exception as e:
            logger.warning(f"获取 TG 更新失败: {e}")
        return []

    def clear_pending_updates(self):
        """清除由于积压的消息"""
        try:
            updates = self.get_updates(timeout=1)
            if updates:
                last_id = updates[-1]["update_id"]
                self.last_update_id = last_id + 1
                self.get_updates(offset=self.last_update_id, timeout=1)
                logger.info("已清理 TG 积压消息")
        except:
            pass


class WeChat:
    """自定义微信通知类 (兼容 Nodeloc/LinuxDo 脚本配置)"""
    def __init__(self):
        self.url = WECHAT_API_URL
        self.token = WECHAT_AUTH_TOKEN
        self.ok = bool(self.url and self.token)

    def send(self, content):
        """发送文本消息"""
        if not self.ok:
            return
        
        params = {
            "token": self.token,
            "title": "ClawCloud 保活通知",
            "content": content
        }
        
        try:
            # 优先尝试 GET 请求
            resp = requests.get(self.url, params=params, timeout=10)
            
            # 如果 GET 失败 (405 Method Not Allowed), 尝试 POST
            if resp.status_code == 405:
                resp = requests.post(self.url, json=params, timeout=10)
            
            if resp.status_code >= 400:
                logger.warning(f"微信通知发送失败 HTTP {resp.status_code}: {resp.text[:100]}")
            else:
                logger.info("微信通知发送成功")
                
        except Exception as e:
            logger.warning(f"微信通知异常: {e}")


class AutoLogin:
    """ClawCloud 自动登录和保活类"""
    
    def __init__(self, account, account_index):
        self.logs = []
        self.shots = []
        self.n = 0
        self.used_old_cookie = False
        self.authenticator_2fa = False
        self.github_mobile_2fa = False
        self.username = account["username"]
        self.password = account["password"]
        self.totp_secret = account.get("totp_secret", "").strip()  # 2FA 密钥
        self.account_index = account_index
        self.cookie_file = os.path.join(
            SCRIPT_DIR,
            f"cookies_{self.username.replace('@', '_').replace('.', '_')}.json"
        )
        self.tg = Telegram()
        self.wx = WeChat()
        self.old_cookies = self.load_cookies()
        self.balance = "未知"
        self.success = True
        self.notify_content = ""
        self.driver = None

    def log(self, msg, level="INFO"):
        """记录日志"""
        icons = {"INFO": "😲", "SUCCESS": "✅", "ERROR": "❌", "WARN": "⚠️", "STEP": "😃"}
        line = f"{icons.get(level, '•')} [{self.username}] {msg}"
        logger.info(line)
        self.logs.append(msg)

    def shot(self, name, push_to_tg=False, caption=""):
        """截图"""
        if not (push_to_tg or "两步验证" in name or "失败" in name):
            return None
        
        self.n += 1
        filename = f"{self.n:02d}_{self.username[:8]}_{name}.png"
        filepath = os.path.join(SCRIPT_DIR, filename)
        
        try:
            self.driver.save_screenshot(filepath)
            self.shots.append(filepath)
            if push_to_tg:
                self.tg.photo(filepath, caption or name)
            return filepath
        except Exception as e:
            logger.warning(f"截图失败: {e}")
        return None

    def load_cookies(self):
        """加载本地 Cookie"""
        if not os.path.exists(self.cookie_file):
            self.log("未检测到本地 Cookies，将进行登录", "INFO")
            return None
        try:
            with open(self.cookie_file, "r", encoding="utf-8") as f:
                cookies = json.load(f)
                if cookies:
                    self.log("检测到本地 Cookies，尝试复用", "INFO")
                    return cookies
        except Exception as e:
            logger.warning(f"加载 Cookie 失败: {e}")
        return None

    def save_cookies(self, cookies):
        """保存 Cookie"""
        if not cookies:
            return
        try:
            with open(self.cookie_file, "w", encoding="utf-8") as f:
                json.dump(cookies, f, indent=2, ensure_ascii=False)
            self.log("已保存最新 Cookies", "SUCCESS")
        except Exception as e:
            logger.warning(f"保存 Cookie 失败: {e}")

    def is_logged_in(self):
        """检测是否已登录"""
        self.log("正在检测是否已登录到仪表盘...", "INFO")
        
        for attempt in range(3):  # 减少尝试次数从5到3
            try:
                # 检查是否在登录页
                if "/signin" in self.driver.current_url:
                    return False
                
                # 检查是否有 GitHub 登录按钮
                try:
                    github_btns = self.driver.find_elements(By.XPATH, "//button[contains(text(), 'GitHub')] | //a[contains(text(), 'GitHub')]")
                    if github_btns:
                        return False
                except:
                    pass
                
                # 检查仪表盘元素(减少等待时间)
                selectors = [
                    (By.XPATH, "//*[contains(text(), 'App Launchpad')]"),
                    (By.XPATH, "//*[contains(text(), 'Database')]"),
                    (By.XPATH, "//*[contains(text(), 'Devbox')]"),
                    (By.XPATH, "//*[contains(text(), 'Object Storage')]"),
                    (By.XPATH, "//*[contains(text(), 'Terminal')]"),
                    (By.CSS_SELECTOR, "input[placeholder*='Search']"),
                    (By.XPATH, "//*[contains(text(), 'Germany')]"),
                    (By.XPATH, "//*[contains(text(), 'Japan')]"),
                ]
                
                for by, selector in selectors:
                    try:
                        elem = WebDriverWait(self.driver, 10).until(  # 减少从15到10秒
                            EC.visibility_of_element_located((by, selector))
                        )
                        if elem:
                            self.log(f"第 {attempt+1} 次检测成功: 找到元素 {selector}", "SUCCESS")
                            return True
                    except:
                        continue
                        
            except Exception as e:
                logger.debug(f"检测异常: {e}")
            
            self.log(f"第 {attempt+1} 次检测未通过，等待重试...", "WARN")
            time.sleep(5)  # 减少从8到5秒
            
            try:
                self.driver.refresh()
                time.sleep(3)  # 减少从5到3秒
            except:
                pass
        
        return False

    def wait_for_2fa_code_via_telegram(self, max_wait=180):
        """通过 TG 等待 2FA 验证码"""
        if not self.tg.ok:
            self.log("未配置 TG 机器人，无法进行交互式验证", "ERROR")
            return False

        self.tg.clear_pending_updates()

        caption = (
            f"⚠️ 【第{self.account_index}个账号】GitHub 两步验证（Authenticator app）\n\n"
            "⚠️ 检测到未配置 totp_secret (2FA密钥)\n"
            "请立即查看 Google Authenticator / Authy 等当前 6 位动态码\n"
            "👉 直接在本 TG 对话框回复数字（例如：123456）\n"
            "🤖 脚本收到后会立即自动填写并提交\n"
            f"⏳ 最多等待 {max_wait} 秒"
        )
        self.shot("两步验证页面", push_to_tg=True, caption=caption)
        self.tg.send("🚀 正在等待您回复验证码...（回复后立即自动填写）")

        self.authenticator_2fa = True
        start_time = time.time()

        while time.time() - start_time < max_wait:
            updates = self.tg.get_updates(offset=self.tg.last_update_id, timeout=10)
            for update in updates:
                self.tg.last_update_id = update["update_id"] + 1
                message = update.get("message", {})
                
                if message.get("chat", {}).get("id") == self.tg.chat_id and "text" in message:
                    text = message["text"].strip()
                    
                    if re.fullmatch(r'\d{6}', text):
                        self.tg.send(f"✅ 收到验证码：{text}，立即自动填写并提交...")
                        
                        try:
                            # 查找 OTP 输入框
                            otp_input = None
                            selectors = [
                                "input#otp",
                                "input[name='otp']",
                                "input[placeholder='XXXXXX']",
                                "input[autocomplete='one-time-code']",
                                "input[type='tel']",
                            ]
                            
                            for sel in selectors:
                                try:
                                    otp_input = WebDriverWait(self.driver, 5).until(
                                        EC.visibility_of_element_located((By.CSS_SELECTOR, sel))
                                    )
                                    if otp_input:
                                        break
                                except:
                                    continue
                            
                            if not otp_input:
                                raise Exception("未找到 OTP 输入框")
                            
                            # 填写验证码
                            otp_input.clear()
                            otp_input.send_keys(text)
                            
                            # 尝试点击提交按钮
                            try:
                                verify_btn = self.driver.find_element(By.XPATH, "//button[contains(text(), 'Verify')]")
                                verify_btn.click()
                            except:
                                # 如果没有按钮,按回车
                                otp_input.send_keys(Keys.RETURN)
                            
                            self.log(f"已立即使用验证码 {text} 自动填写并提交", "SUCCESS")
                            self.tg.send(f"✅ 已自动填写验证码 {text} 并提交")
                            time.sleep(8)
                            return True
                            
                        except Exception as e:
                            self.log(f"自动填写失败: {e}", "ERROR")
                            self.tg.send("❌ 自动填写失败，请手动输入当前验证码完成登录")
                            return False
            
            time.sleep(0.5)
        
        self.tg.send("⏰ 等待超时，未收到验证码（请手动完成验证）")
        self.log("等待验证码超时", "WARN")
        return False

    def full_github_login(self):
        """执行完整 GitHub 登录流程"""
        self.log("执行完整 GitHub 登录流程", "STEP")
        
        # 查找 GitHub 登录按钮
        try:
            login_btn = WebDriverWait(self.driver, 10).until(
                EC.element_to_be_clickable((By.XPATH, "//button[contains(text(), 'GitHub')] | //a[contains(text(), 'GitHub')]"))
            )
        except:
            self.log("未找到登录按钮，说明已登录", "SUCCESS")
            return
        
        self.shot("ClawCloud登录界面")
        login_btn.click()
        self.log("已点击 GitHub 登录按钮", "SUCCESS")
        time.sleep(3)
        
        # 检查是否直接到授权页
        try:
            WebDriverWait(self.driver, 15).until(
                lambda d: "oauth/authorize" in d.current_url
            )
            self.log("检测到 GitHub 授权页面", "SUCCESS")
            self.shot("GitHub授权页")
            
            try:
                auth_btn = WebDriverWait(self.driver, 10).until(
                    EC.element_to_be_clickable((By.XPATH, "//button[contains(text(), 'Authorize')]"))
                )
                auth_btn.click()
                self.log("✅ 已自动授权 ClawCloud", "SUCCESS")
                
                # 等待跳转回 ClawCloud
                try:
                    WebDriverWait(self.driver, 30).until(
                        lambda d: CLAW_CLOUD_URL in d.current_url
                    )
                except:
                    self.driver.get(CLAW_CLOUD_URL)
                
                time.sleep(10)
                self.driver.refresh()
                time.sleep(5)
                self.log("授权完成，已强制刷新仪表盘", "SUCCESS")
                return
                
            except Exception as e:
                logger.debug(f"授权异常: {e}")
                
        except TimeoutException:
            self.log("未检测到授权页，可能需要密码登录", "INFO")
        
        # 检查是否需要密码登录
        time.sleep(3)
        if "github.com/login" in self.driver.current_url:
            self.log("进入 GitHub 密码登录页面", "INFO")
            
            try:
                # 填写账号密码
                username_field = WebDriverWait(self.driver, 10).until(
                    EC.presence_of_element_located((By.ID, "login_field"))
                )
                username_field.clear()
                username_field.send_keys(self.username)
                
                password_field = self.driver.find_element(By.ID, "password")
                password_field.clear()
                password_field.send_keys(self.password)
                
                submit_btn = self.driver.find_element(By.CSS_SELECTOR, "input[type='submit']")
                submit_btn.click()
                
                self.log("✅ 已提交账号密码", "SUCCESS")
                self.shot("提交密码后")
                time.sleep(5)
                
                # 检查 2FA
                try:
                    WebDriverWait(self.driver, 10).until(
                        EC.presence_of_element_located((By.XPATH, "//*[contains(text(), 'Two-factor authentication')]"))
                    )
                    self.log("⚠️ 检测到两步验证", "WARN")
                    
                    # 检查是 Authenticator 还是 GitHub Mobile
                    page_text = self.driver.page_source
                    
                    if "Enter the code from your two-factor authentication app" in page_text:
                        # Authenticator app - 使用 pyotp 自动生成 或 TG 交互
                        self.authenticator_2fa = True
                        
                        if not self.totp_secret:
                            # 没有配置密钥,发送 TG/微信 通知
                            caption = (
                                f"⚠️ 【第{self.account_index}个账号】检测到 GitHub 两步验证\n\n"
                                "未配置 totp_secret,无法自动填写验证码\n"
                                "请手动输入验证码或配置 2FA 密钥"
                            )
                            self.shot("两步验证页面", push_to_tg=True, caption=caption)
                            self.log("未配置 2FA 密钥,等待60秒手动输入", "WARN")
                            time.sleep(60)
                        else:
                            # 使用 pyotp 自动生成验证码
                            try:
                                token = pyotp.TOTP(self.totp_secret).now()
                                self.log(f"生成 2FA 验证码: {token}", "INFO")
                                
                                # 查找 OTP 输入框
                                otp_input = None
                                selectors = [
                                    "input#otp",
                                    "input[name='otp']",
                                    "input[placeholder='XXXXXX']",
                                    "input[autocomplete='one-time-code']",
                                    "input[type='tel']",
                                ]
                                
                                for sel in selectors:
                                    try:
                                        otp_input = WebDriverWait(self.driver, 5).until(
                                            EC.visibility_of_element_located((By.CSS_SELECTOR, sel))
                                        )
                                        if otp_input:
                                            break
                                    except:
                                        continue
                                
                                if not otp_input:
                                    raise Exception("未找到 OTP 输入框")
                                
                                # 填写验证码
                                otp_input.clear()
                                time.sleep(0.5)
                                
                                # 逐个字符输入
                                for char in token:
                                    otp_input.send_keys(char)
                                    time.sleep(0.1)
                                
                                self.log("已输入 2FA 验证码", "INFO")
                                time.sleep(1)
                                
                                # 查找并点击提交按钮
                                try:
                                    submit_selectors = [
                                        "button[type='submit']",
                                        "input[type='submit']",
                                        "button.btn-primary"
                                    ]
                                    
                                    submitted = False
                                    for selector in submit_selectors:
                                        try:
                                            submit_btn = self.driver.find_element(By.CSS_SELECTOR, selector)
                                            submit_btn.click()
                                            self.log(f"已点击提交按钮: {selector}", "INFO")
                                            submitted = True
                                            break
                                        except:
                                            continue
                                    
                                    if not submitted:
                                        # 如果找不到按钮,尝试按回车
                                        otp_input = self.driver.find_element(By.CSS_SELECTOR, selectors[0])
                                        otp_input.send_keys(Keys.RETURN)
                                        self.log("已按回车提交", "INFO")
                                        
                                except Exception as e:
                                    logger.warning(f"提交方式失败: {e}")
                                    # 最后的尝试:直接提交表单
                                    try:
                                        otp_input = self.driver.find_element(By.CSS_SELECTOR, selectors[0])
                                        self.driver.execute_script("arguments[0].form.submit();", otp_input)
                                        self.log("已通过 JS 提交表单", "INFO")
                                    except:
                                        pass
                                
                                time.sleep(5)
                                self.log("✅ 2FA 验证码已自动填写并提交", "SUCCESS")
                                
                            except Exception as e:
                                self.log(f"2FA 自动填写失败: {e}", "ERROR")
                                self.shot("2FA失败页面", push_to_tg=True, caption=f"❌ 2FA 自动填写失败: {e}")
                                time.sleep(30)
                    else:
                        # GitHub Mobile
                        self.github_mobile_2fa = True
                        caption = (
                            f"⚠️ 【第{self.account_index}个账号】检测到 GitHub 两步验证（GitHub Mobile）\n\n"
                            "请打开手机 GitHub App，批准登录请求\n"
                            "脚本已等待60秒供您操作，完成后会自动继续"
                        )
                        self.shot("两步验证页面", push_to_tg=True, caption=caption)
                        self.log("等待60秒让你手动批准 GitHub Mobile 2FA...", "WARN")
                        
                        try:
                            WebDriverWait(self.driver, 60).until(
                                lambda d: "oauth/authorize" in d.current_url or CLAW_CLOUD_URL in d.current_url
                            )
                            self.log("2FA 批准成功，继续流程", "SUCCESS")
                        except TimeoutException:
                            self.log("2FA 等待超时，尝试强制继续", "WARN")
                            
                except TimeoutException:
                    self.log("未检测到 2FA，继续流程", "INFO")
                
                # 再次检查授权页
                time.sleep(5)
                if "oauth/authorize" in self.driver.current_url:
                    try:
                        auth_btn = WebDriverWait(self.driver, 10).until(
                            EC.element_to_be_clickable((By.XPATH, "//button[contains(text(), 'Authorize')]"))
                        )
                        auth_btn.click()
                        self.log("✅ 密码后自动授权", "SUCCESS")
                        time.sleep(5)
                    except:
                        pass
                
            except Exception as e:
                self.log(f"密码登录异常: {e}", "ERROR")
                self.shot("登录异常页面")
        
        # 确保返回 ClawCloud
        try:
            WebDriverWait(self.driver, 30).until(
                lambda d: CLAW_CLOUD_URL in d.current_url
            )
            self.log("已跳转回 ClawCloud", "SUCCESS")
        except:
            self.log("未自动返回，强制跳转首页", "WARN")
            self.driver.get(CLAW_CLOUD_URL)
        
        # 增加等待时间,确保页面完全加载
        time.sleep(10)
        self.driver.refresh()
        time.sleep(10)
        self.log("已强制刷新，确保仪表盘完全加载", "SUCCESS")

    def keepalive(self):
        """保活访问"""
        self.log("开始保活访问...", "STEP")
        
        urls = [
            (f"{CLAW_CLOUD_URL}/", "首页"),
            (f"{CLAW_CLOUD_URL}/apps", "Apps页面")
        ]
        
        for url, name in urls:
            try:
                self.driver.get(url)
                time.sleep(5)
                self.log(f"保活访问: {name}", "SUCCESS")
            except Exception as e:
                self.log(f"访问失败: {e}", "WARN")

    def generate_notify_content(self):
        """生成通知内容"""
        if self.used_old_cookie:
            login_way = "使用Cookies授权登录"
        elif self.authenticator_2fa:
            login_way = "Authenticator app自动登录"
        elif self.github_mobile_2fa:
            login_way = "GitHub Mobile手动批准登录"
        else:
            login_way = "使用Cookies授权登录"

        display_user = self.username[:3] + "**" if "@" not in self.username[:3] else self.username.split("@")[0][:3] + "**"
        balance_display = self.balance if self.balance.startswith('$') else f"${self.balance}"

        important_lines = []
        priority_keywords = [
            "已强制刷新，确保仪表盘完全加载",
            r"第 \d+ 次检测成功: 找到元素",
            "已保存最新 Cookies"
        ]
        
        for keyword in priority_keywords:
            pattern = re.compile(keyword)
            for log in self.logs:
                if pattern.search(log):
                    important_lines.append(log)
                    break

        result_text = "✅ 成功" if self.success else "❌ 失败"

        content = f"登录逻辑： {login_way}\n"
        content += f"用户： {display_user}\n"
        content += "重要信息：\n"
        for line in important_lines[:3]:
            if line:
                content += f"✅ [第{self.account_index}个账号] {line}\n"
        content += f"💵当前剩余：{balance_display}\n"
        content += f"保活结果： {result_text}\n"
        content += f"时间： {time.strftime('%Y-%m-%d %H:%M:%S')}"

        self.notify_content = content

    def cleanup_screenshots(self):
        """清理截图"""
        deleted = 0
        for p in self.shots:
            try:
                if os.path.exists(p):
                    os.remove(p)
                    deleted += 1
            except:
                pass
        if deleted > 0:
            self.log(f"已清理 {deleted} 张截图", "SUCCESS")

    def find_chrome(self):
        """查找 Chromium"""
        candidates = [
            "/usr/bin/chromium",
            "/usr/bin/chromium-browser",
            "/usr/bin/google-chrome",
        ]
        for path in candidates:
            if os.path.exists(path):
                return path
        return None

    def find_chromedriver(self):
        """查找 ChromeDriver"""
        candidates = [
            "/usr/bin/chromedriver",
            "/usr/local/bin/chromedriver",
        ]
        for path in candidates:
            if os.path.exists(path):
                return path
        return None

    def run(self):
        """运行保活流程"""
        self.log("开始运行保活流程", "STEP")
        
        # 配置浏览器
        chrome_path = self.find_chrome()
        if not chrome_path:
            self.log("未找到 Chromium", "ERROR")
            self.success = False
            self.generate_notify_content()
            return self.notify_content
        
        options = Options()
        options.add_argument("--headless")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--disable-gpu")
        options.add_argument("--disable-blink-features=AutomationControlled")
        options.add_argument("--window-size=1920,1080")
        options.add_experimental_option("excludeSwitches", ["enable-automation"])
        options.add_experimental_option("useAutomationExtension", False)
        
        # 配置浏览器代理
        if CLAW_PROXY:
            options.add_argument(f"--proxy-server={CLAW_PROXY}")
            
        options.binary_location = chrome_path
        
        try:
            # 启动浏览器
            chromedriver_path = self.find_chromedriver()
            if chromedriver_path:
                service = Service(executable_path=chromedriver_path)
                self.driver = webdriver.Chrome(service=service, options=options)
            else:
                self.driver = webdriver.Chrome(options=options)
            
            self.log("浏览器启动成功", "SUCCESS")
            
            # 移除 webdriver 标识
            self.driver.execute_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
            )
            
            # 注入 Cookie
            if self.old_cookies:
                self.driver.get(CLAW_CLOUD_URL)
                time.sleep(2)
                
                for cookie in self.old_cookies:
                    try:
                        self.driver.add_cookie(cookie)
                    except:
                        pass
                
                self.log("已注入本地 Cookies", "SUCCESS")
            
            # 访问主页
            self.driver.get(CLAW_CLOUD_URL)
            time.sleep(10)
            self.shot("打开主页后")
            
            # 检查登录状态
            if self.is_logged_in():
                self.log("🎉 已登录，直接保活", "SUCCESS")
                self.used_old_cookie = True
            else:
                self.log("检测到未登录，执行登录流程", "WARN")
                self.full_github_login()
                
                if self.is_logged_in():
                    self.log("✅ 登录/授权最终成功！", "SUCCESS")
                else:
                    self.log("❌ 登录最终失败", "ERROR")
                    self.success = False
                    self.shot("最终失败页面", push_to_tg=True, caption="❌ 保活失败，请手动检查")
                    self.generate_notify_content()
                    return self.notify_content
            
            # 提取余额
            try:
                balance_elem = WebDriverWait(self.driver, 10).until(
                    EC.presence_of_element_located((By.XPATH, "//*[contains(text(), '$')]"))
                )
                raw_balance = balance_elem.text.strip()
                # 提取 $ 符号和数字
                match = re.search(r'\$[\d.,]+', raw_balance)
                if match:
                    self.balance = match.group()
                    self.log(f"成功提取余额: {self.balance}", "SUCCESS")
                else:
                    self.balance = raw_balance
            except:
                self.balance = "提取失败"
                self.log("未能提取到余额", "WARN")
            
            # 保存 Cookie
            current_cookies = self.driver.get_cookies()
            if current_cookies:
                # 只保存相关域名的 Cookie
                filtered_cookies = [
                    c for c in current_cookies 
                    if 'github.com' in c.get('domain', '') or 'claw.cloud' in c.get('domain', '')
                ]
                if filtered_cookies:
                    self.save_cookies(filtered_cookies)
            
            # 保活访问
            self.keepalive()
            
            # 生成通知
            self.generate_notify_content()
            
        except Exception as e:
            self.log(f"运行异常: {e}", "ERROR")
            logger.exception(e)
            self.success = False
            self.generate_notify_content()
            
        finally:
            if self.driver:
                try:
                    self.driver.quit()
                except:
                    pass
            
            self.cleanup_screenshots()
        
        return self.notify_content


if __name__ == "__main__":
    print("\n" + "="*60)
    print("💻 ClawCloud多账号自动保活 - Selenium 版本")
    print("="*60 + "\n")

    # 检查账号配置
    if not ACCOUNTS:
        print("❌ 错误: 未配置任何账号!")
        print("\n请在青龙面板的环境变量中配置 CLAW_ACCOUNTS")
        print("格式: 账号1@example.com----密码1----2FA密钥1&账号2@example.com----密码2----2FA密钥2")
        print("\n或在脚本中修改 ACCOUNTS_CONFIG 变量\n")
        sys.exit(1)
    
    print(f"📊 共配置 {len(ACCOUNTS)} 个账号\n")

    all_notify_contents = []
    has_screenshot_triggered = False

    for idx, acc in enumerate(ACCOUNTS, 1):
        print(f"正在处理第 {idx} 个账号: {acc['username']}")
        instance = AutoLogin(acc, idx)
        content = instance.run()
        
        if content:
            all_notify_contents.append(f"【账号{idx}保活信息】\n{content}")

        if instance.shots:
            has_screenshot_triggered = True

        print(f"第 {idx} 个账号处理完成\n")
        time.sleep(10)

    # 发送汇总通知
    if all_notify_contents:
        final_msg = f"💻 ClawCloud自动保活 - Selenium版本\n\n"
        final_msg += f"🔥一共有{len(ACCOUNTS)}个账号🔥\n\n"
        final_msg += "\n\n==========================\n\n".join(all_notify_contents)
        final_msg += "\n\n==========================\n\n"
        final_msg += "🗑️ 本次运行截图已清理\n" if has_screenshot_triggered else "🗑️ 本次运行脚本没有触发截图\n"
        final_msg += "\n\n==========================\n\n"
        final_msg += f"网页登录地址：{CLAW_CLOUD_URL}\n"
        final_msg += "\n\n==========================\n\n"

        tg = Telegram()
        tg.send(final_msg)
        
        # 发送企微通知
        wx = WeChat()
        wx.send(final_msg)
    
    print("\n" + "="*60)
    print("✅ 所有账号处理完成")
    print("="*60 + "\n")
