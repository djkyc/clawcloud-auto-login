# 文件名: login_script.py
# 作用: 自动登录 ClawCloud Run，支持 GitHub 账号密码 + 2FA 自动验证
# 仅新增：Telegram 接收消息（成功 / 失败样本）

import os
import time
import pyotp
import requests
from datetime import datetime
from playwright.sync_api import sync_playwright


def mask_account(account: str) -> str:
    """邮箱脱敏"""
    if not account or "@" not in account:
        return "unknown"
    name, domain = account.split("@", 1)
    if len(name) <= 3:
        return f"{name[0]}***@{domain}"
    return f"{name[:3]}***@{domain}"


def send_tg_message(text: str):
    bot_token = os.environ.get("TG_BOT_TOKEN")
    chat_id = os.environ.get("TG_CHAT_ID")

    if not bot_token or not chat_id:
        print("ℹ️ 未配置 TG_BOT_TOKEN / TG_CHAT_ID，跳过 TG 通知")
        return

    try:
        requests.post(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": text
            },
            timeout=10
        )
    except Exception as e:
        print(f"⚠️ TG 消息发送失败: {e}")


def run_login():
    username = os.environ.get("GH_USERNAME")
    password = os.environ.get("GH_PASSWORD")
    totp_secret = os.environ.get("GH_2FA_SECRET")

    now_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    masked_user = mask_account(username)

    if not username or not password:
        msg = (
            "❌ ClawCloud 登录失败\n\n"
            f"👤 账号：{masked_user}\n"
            f"🕒 时间：{now_time}\n"
            "⚠️ 原因：缺少 GH_USERNAME 或 GH_PASSWORD"
        )
        print(msg)
        send_tg_message(msg)
        return

    print("🚀 [Step 1] 启动浏览器...")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(viewport={'width': 1920, 'height': 1080})
        page = context.new_page()

        target_url = "https://ap-northeast-1.run.claw.cloud/"
        print(f"🌐 [Step 2] 正在访问: {target_url}")
        page.goto(target_url)
        page.wait_for_load_state("networkidle")

        print("🔍 [Step 3] 寻找 GitHub 按钮...")
        try:
            page.locator("button:has-text('GitHub')").click(timeout=10000)
        except:
            pass

        print("⏳ [Step 4] 等待跳转到 GitHub...")
        try:
            page.wait_for_url(lambda url: "github.com" in url, timeout=15000)
            if "login" in page.url:
                page.fill("#login_field", username)
                page.fill("#password", password)
                page.click("input[name='commit']")
        except:
            pass

        page.wait_for_timeout(3000)

        if "two-factor" in page.url or page.locator("#app_totp").count() > 0:
            print("🔐 [Step 5] 检测到 2FA")
            if totp_secret:
                try:
                    token = pyotp.TOTP(totp_secret).now()
                    page.fill("#app_totp", token)
                except Exception as e:
                    msg = (
                        "❌ ClawCloud 登录失败\n\n"
                        f"👤 账号：{masked_user}\n"
                        f"🕒 时间：{now_time}\n"
                        f"⚠️ 原因：2FA 验证码填写失败\n{e}"
                    )
                    print(msg)
                    send_tg_message(msg)
            else:
                msg = (
                    "🚨 ClawCloud 登录中断（致命）\n\n"
                    f"👤 账号：{masked_user}\n"
                    f"🕒 时间：{now_time}\n"
                    "❌ 检测到 2FA 但未配置 GH_2FA_SECRET"
                )
                print(msg)
                send_tg_message(msg)
                exit(1)

        page.wait_for_timeout(3000)
        if "authorize" in page.url.lower():
            try:
                page.click("button:has-text('Authorize')", timeout=5000)
            except:
                pass

        print("⏳ [Step 6] 等待跳转回 ClawCloud 控制台...")
        page.wait_for_timeout(20000)

        final_url = page.url
        page.screenshot(path="login_result.png")

        is_success = False
        if page.get_by_text("App Launchpad").count() > 0:
            is_success = True
        elif page.get_by_text("Devbox").count() > 0:
            is_success = True
        elif "private-team" in final_url or "console" in final_url:
            is_success = True
        elif "signin" not in final_url and "github.com" not in final_url:
            is_success = True

        if is_success:
            msg = (
                "🎉 ClawCloud 登录成功\n\n"
                f"👤 账号：{masked_user}\n"
                f"🕒 时间：{now_time}\n"
                "🌐 控制台：\n"
                f"{final_url}"
            )
            print(msg)
            send_tg_message(msg)
        else:
            msg = (
                "❌ ClawCloud 登录失败\n\n"
                f"👤 账号：{masked_user}\n"
                f"🕒 时间：{now_time}\n"
                "⚠️ 原因：GitHub 登录或 2FA 未通过\n\n"
                "📸 已生成调试截图：login_result.png"
            )
            print(msg)
            send_tg_message(msg)
            exit(1)

        browser.close()


if __name__ == "__main__":
    run_login()
