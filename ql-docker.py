#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ClawCloud Run 自动登录脚本 - Selenium 版本
适配青龙面板 ARM Docker 环境
支持 GitHub OAuth + 2FA 自动验证
"""

import os
import time
import random
import pyotp
import requests
from datetime import datetime
from loguru import logger
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC


def mask_account(account: str) -> str:
    """邮箱脱敏"""
    if not account or "@" not in account:
        return "unknown"
    name, domain = account.split("@", 1)
    if len(name) <= 3:
        return f"{name[0]}***@{domain}"
    return f"{name[:3]}***@{domain}"


def send_tg_message(text: str):
    """发送 Telegram 通知"""
    bot_token = os.environ.get("TG_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TG_CHAT_ID", "").strip()

    if not bot_token or not chat_id:
        logger.info("未配置 TG_BOT_TOKEN / TG_CHAT_ID，跳过 TG 通知")
        return

    try:
        r = requests.post(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": text,
                "disable_web_page_preview": True
            },
            timeout=10
        )
        if r.status_code == 200:
            logger.info("TG 通知发送成功")
        else:
            logger.warning(f"TG 通知发送失败 HTTP={r.status_code}")
    except Exception as e:
        logger.warning(f"TG 消息发送失败: {e}")


def find_chrome():
    """查找 Chromium 可执行文件"""
    candidates = [
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
        "/usr/bin/google-chrome",
        "/usr/bin/google-chrome-stable",
    ]
    
    for path in candidates:
        if os.path.exists(path):
            return path
    
    return None


def find_chromedriver():
    """查找 ChromeDriver"""
    candidates = [
        "/usr/bin/chromedriver",
        "/usr/local/bin/chromedriver",
    ]
    
    for path in candidates:
        if os.path.exists(path):
            return path
    
    return None


def run_login():
    """执行登录流程"""
    username = os.environ.get("GH_USERNAME", "").strip()
    password = os.environ.get("GH_PASSWORD", "").strip()
    totp_secret = os.environ.get("GH_2FA_SECRET", "").strip()

    now_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    masked_user = mask_account(username)

    if not username or not password:
        msg = (
            "❌ ClawCloud 登录失败\n\n"
            f"👤 账号：{masked_user}\n"
            f"🕒 时间：{now_time}\n"
            "⚠️ 原因：缺少 GH_USERNAME 或 GH_PASSWORD"
        )
        logger.error(msg)
        send_tg_message(msg)
        return False

    logger.info("=" * 60)
    logger.info("🚀 ClawCloud 自动登录开始")
    logger.info(f"👤 账号：{masked_user}")
    logger.info(f"🕒 时间：{now_time}")
    logger.info("=" * 60)

    # 配置浏览器
    logger.info("[Step 1] 配置浏览器...")
    
    chrome_path = find_chrome()
    if not chrome_path:
        msg = (
            "❌ ClawCloud 登录失败\n\n"
            f"👤 账号：{masked_user}\n"
            f"🕒 时间：{now_time}\n"
            "⚠️ 原因：未找到 Chromium 可执行文件"
        )
        logger.error(msg)
        send_tg_message(msg)
        return False
    
    logger.info(f"使用 Chrome 路径: {chrome_path}")

    options = Options()
    options.add_argument("--headless")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument("--window-size=1920,1080")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)
    
    options.binary_location = chrome_path

    driver = None
    try:
        # 启动浏览器
        chromedriver_path = find_chromedriver()
        if chromedriver_path:
            logger.info(f"使用 ChromeDriver 路径: {chromedriver_path}")
            service = Service(executable_path=chromedriver_path)
            driver = webdriver.Chrome(service=service, options=options)
        else:
            logger.warning("未找到 chromedriver,尝试自动查找")
            driver = webdriver.Chrome(options=options)

        logger.success("浏览器启动成功")

        # 移除 webdriver 标识
        driver.execute_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )

        # 访问 ClawCloud
        target_url = "https://ap-northeast-1.run.claw.cloud/"
        logger.info(f"[Step 2] 正在访问: {target_url}")
        driver.get(target_url)
        time.sleep(random.randint(3, 5))

        # 查找并点击 GitHub 按钮
        logger.info("[Step 3] 寻找 GitHub 登录按钮...")
        try:
            github_btn = WebDriverWait(driver, 10).until(
                EC.element_to_be_clickable((By.XPATH, "//button[contains(text(), 'GitHub')]"))
            )
            github_btn.click()
            logger.info("已点击 GitHub 按钮")
            time.sleep(random.randint(2, 4))
        except Exception as e:
            logger.warning(f"未找到 GitHub 按钮: {e}")

        # 等待跳转到 GitHub
        logger.info("[Step 4] 等待跳转到 GitHub...")
        time.sleep(3)

        # 检查是否在 GitHub 登录页
        if "github.com" in driver.current_url and "login" in driver.current_url:
            logger.info("检测到 GitHub 登录页,填写账号密码")
            
            try:
                # 填写用户名
                username_field = WebDriverWait(driver, 10).until(
                    EC.presence_of_element_located((By.ID, "login_field"))
                )
                username_field.clear()
                username_field.send_keys(username)
                time.sleep(random.uniform(0.5, 1.5))
                
                # 填写密码
                password_field = driver.find_element(By.ID, "password")
                password_field.clear()
                password_field.send_keys(password)
                time.sleep(random.uniform(0.5, 1.5))
                
                # 点击登录
                login_btn = driver.find_element(By.CSS_SELECTOR, "input[name='commit']")
                login_btn.click()
                logger.info("已提交登录表单")
                time.sleep(random.randint(3, 5))
                
            except Exception as e:
                logger.error(f"填写登录表单失败: {e}")

        # 检查 2FA
        time.sleep(2)
        if "two-factor" in driver.current_url or "two_factor" in driver.current_url:
            logger.info("[Step 5] 检测到 2FA 验证")
            
            if not totp_secret:
                msg = (
                    "🚨 ClawCloud 登录中断（致命）\n\n"
                    f"👤 账号：{masked_user}\n"
                    f"🕒 时间：{now_time}\n"
                    "❌ 检测到 2FA 但未配置 GH_2FA_SECRET"
                )
                logger.error(msg)
                send_tg_message(msg)
                driver.save_screenshot("/ql/data/scripts/clawcloud_2fa_error.png")
                return False
            
            try:
                # 生成 TOTP 验证码
                token = pyotp.TOTP(totp_secret).now()
                logger.info(f"生成 2FA 验证码: {token}")
                
                # 等待并填写验证码
                totp_field = WebDriverWait(driver, 10).until(
                    EC.presence_of_element_located((By.ID, "app_totp"))
                )
                
                # 清空并输入验证码
                totp_field.clear()
                time.sleep(0.5)
                
                # 逐个字符输入,避免问题
                for char in token:
                    totp_field.send_keys(char)
                    time.sleep(0.1)
                
                logger.info("已输入 2FA 验证码")
                time.sleep(1)
                
                # 查找并点击提交按钮(而不是直接 submit)
                try:
                    # 尝试查找提交按钮
                    submit_selectors = [
                        "button[type='submit']",
                        "input[type='submit']",
                        "button.btn-primary"
                    ]
                    
                    submitted = False
                    for selector in submit_selectors:
                        try:
                            submit_btn = driver.find_element(By.CSS_SELECTOR, selector)
                            submit_btn.click()
                            logger.info(f"已点击提交按钮: {selector}")
                            submitted = True
                            break
                        except Exception:
                            continue
                    
                    if not submitted:
                        # 如果找不到按钮,尝试按回车
                        from selenium.webdriver.common.keys import Keys
                        totp_field = driver.find_element(By.ID, "app_totp")
                        totp_field.send_keys(Keys.RETURN)
                        logger.info("已按回车提交")
                        
                except Exception as e:
                    logger.warning(f"提交方式失败,尝试其他方法: {e}")
                    # 最后的尝试:直接提交表单
                    try:
                        totp_field = driver.find_element(By.ID, "app_totp")
                        driver.execute_script("arguments[0].form.submit();", totp_field)
                        logger.info("已通过 JS 提交表单")
                    except Exception:
                        pass
                
                time.sleep(random.randint(3, 5))
                
            except Exception as e:
                msg = (
                    "❌ ClawCloud 登录失败\n\n"
                    f"👤 账号：{masked_user}\n"
                    f"🕒 时间：{now_time}\n"
                    f"⚠️ 原因：2FA 验证码填写失败\n{e}"
                )
                logger.error(msg)
                send_tg_message(msg)
                driver.save_screenshot("/ql/data/scripts/clawcloud_2fa_fail.png")
                return False

        # 检查授权页面
        time.sleep(2)
        if "authorize" in driver.current_url.lower():
            logger.info("[Step 6] 检测到授权页面")
            try:
                authorize_btn = WebDriverWait(driver, 5).until(
                    EC.element_to_be_clickable((By.XPATH, "//button[contains(text(), 'Authorize')]"))
                )
                authorize_btn.click()
                logger.info("已点击授权按钮")
                time.sleep(random.randint(2, 4))
            except Exception as e:
                logger.warning(f"未找到授权按钮: {e}")

        # 等待跳转回 ClawCloud
        logger.info("[Step 7] 等待跳转回 ClawCloud 控制台...")
        time.sleep(20)

        # 检查登录结果
        final_url = driver.current_url
        logger.info(f"最终 URL: {final_url}")
        
        # 保存截图
        driver.save_screenshot("/ql/data/scripts/clawcloud_result.png")
        logger.info("已保存截图: /ql/data/scripts/clawcloud_result.png")

        # 判断是否登录成功
        is_success = False
        
        # 方法1: 检查页面文本
        page_text = driver.page_source.lower()
        if "app launchpad" in page_text or "devbox" in page_text:
            is_success = True
        
        # 方法2: 检查 URL
        if "private-team" in final_url or "console" in final_url:
            is_success = True
        
        # 方法3: 排除登录页
        if "signin" not in final_url and "github.com" not in final_url:
            is_success = True

        if is_success:
            msg = (
                "🎉 ClawCloud 登录成功\n\n"
                f"👤 账号：{masked_user}\n"
                f"🕒 时间：{now_time}\n"
                "🌐 控制台：\n"
                f"{final_url}"
            )
            logger.success(msg)
            send_tg_message(msg)
            return True
        else:
            msg = (
                "❌ ClawCloud 登录失败\n\n"
                f"👤 账号：{masked_user}\n"
                f"🕒 时间：{now_time}\n"
                "⚠️ 原因：GitHub 登录或 2FA 未通过\n\n"
                "📸 已生成调试截图：/ql/data/scripts/clawcloud_result.png"
            )
            logger.error(msg)
            send_tg_message(msg)
            return False

    except Exception as e:
        msg = (
            "❌ ClawCloud 登录异常\n\n"
            f"👤 账号：{masked_user}\n"
            f"🕒 时间：{now_time}\n"
            f"⚠️ 错误：{str(e)}"
        )
        logger.error(msg)
        logger.exception(e)
        send_tg_message(msg)
        
        if driver:
            try:
                driver.save_screenshot("/ql/data/scripts/clawcloud_error.png")
            except Exception:
                pass
        
        return False

    finally:
        if driver:
            try:
                driver.quit()
                logger.info("浏览器已关闭")
            except Exception:
                pass


if __name__ == "__main__":
    logger.info("=" * 60)
    logger.info("ClawCloud 自动登录脚本 - Selenium 版本")
    logger.info("=" * 60)
    
    success = run_login()
    
    if success:
        logger.info("=" * 60)
        logger.success("✅ 登录成功")
        logger.info("=" * 60)
        exit(0)
    else:
        logger.info("=" * 60)
        logger.error("❌ 登录失败")
        logger.info("=" * 60)
        exit(1)
