# slider_debugger.py —— 滑块验证码诊断工具 v1.0
# 当自动破解失败时，手动运行此工具诊断原因
#
# 用法：
#   python slider_debugger.py <目标URL>
#   等待浏览器打开 → 手动触发验证码（翻页/搜索等操作触发滑块）
#   → 回终端按回车 → 自动导出所有canvas信息 + 截图 + DOM结构
#
# 导出文件在 output/slider_debug/ 目录：
#   - canvases_*.json    所有canvas/img元素的尺寸/位置/base64数据
#   - captcha_area_*.png 验证码区域截图
#   - fullpage_*.png     整页截图
#   - dom_snapshot_*.json 验证码相关DOM结构

import os
import sys
import json
import base64
import time
from datetime import datetime

# 添加项目路径
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import CHROME_PATH, CHROMEDRIVER_PATH, get_random_ua, RUNTIME_DIR
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By

OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output", "slider_debug")
os.makedirs(OUT_DIR, exist_ok=True)


def dump_canvas_info(driver, label: str = ""):
    """导出所有canvas/img元素的信息"""
    ts = datetime.now().strftime("%H%M%S")

    # 先检查主文档
    main_result = []
    try:
        canvases = driver.find_elements(By.TAG_NAME, "canvas")
        imgs = driver.find_elements(By.TAG_NAME, "img")

        for c in canvases:
            try:
                r = c.rect
                if r["width"] < 5 or r["height"] < 5:
                    continue
                b64 = driver.execute_script("""
                    try { return arguments[0].toDataURL('image/png'); } catch(e) { return ''; }
                """, c)
                main_result.append({
                    "tag": "canvas",
                    "location": "main_document",
                    "css_size": f"{r['width']:.0f}x{r['height']:.0f}",
                    "natural_size": driver.execute_script(
                        "return arguments[0].width + 'x' + arguments[0].height;", c),
                    "rect": {"x": r["x"], "y": r["y"], "w": r["width"], "h": r["height"]},
                    "has_b64": len(b64) > 100,
                })
            except Exception:
                continue

        for img in imgs:
            try:
                r = img.rect
                if r["width"] < 5 or r["height"] < 5:
                    continue
                main_result.append({
                    "tag": "img",
                    "location": "main_document",
                    "css_size": f"{r['width']:.0f}x{r['height']:.0f}",
                    "src": (img.get_attribute("src") or "")[:120],
                    "rect": {"x": r["x"], "y": r["y"], "w": r["width"], "h": r["height"]},
                })
            except Exception:
                continue
    except Exception as e:
        main_result.append({"error": str(e)})

    # 检查所有iframe
    iframe_results = []
    try:
        iframes = driver.find_elements(By.TAG_NAME, "iframe")
        print(f"\n[*] Found {len(iframes)} iframes")
        for idx, iframe in enumerate(iframes):
            try:
                src = (iframe.get_attribute("src") or "")[:100]
                driver.switch_to.frame(iframe)
                sub_canvases = driver.find_elements(By.TAG_NAME, "canvas")
                sub_imgs = driver.find_elements(By.TAG_NAME, "img")

                iframe_data = {
                    "iframe_index": idx,
                    "src": src,
                    "canvas_count": len(sub_canvases),
                    "img_count": len(sub_imgs),
                    "elements": []
                }

                for c in sub_canvases:
                    try:
                        r = c.rect
                        if r["width"] < 5 or r["height"] < 5:
                            continue
                        b64 = driver.execute_script("""
                            try { return arguments[0].toDataURL('image/png'); } catch(e) { return ''; }
                        """, c)
                        iframe_data["elements"].append({
                            "tag": "canvas",
                            "css_size": f"{r['width']:.0f}x{r['height']:.0f}",
                            "natural_size": driver.execute_script(
                                "return arguments[0].width + 'x' + arguments[0].height;", c),
                            "rect": {"x": r["x"], "y": r["y"], "w": r["width"], "h": r["height"]},
                            "has_b64": len(b64) > 100,
                        })
                    except Exception:
                        continue

                for img in sub_imgs:
                    try:
                        r = img.rect
                        if r["width"] < 5 or r["height"] < 5:
                            continue
                        iframe_data["elements"].append({
                            "tag": "img",
                            "css_size": f"{r['width']:.0f}x{r['height']:.0f}",
                            "src": (img.get_attribute("src") or "")[:150],
                            "rect": {"x": r["x"], "y": r["y"], "w": r["width"], "h": r["height"]},
                        })
                    except Exception:
                        continue

                iframe_results.append(iframe_data)
                driver.switch_to.default_content()
            except Exception as e:
                iframe_results.append({"iframe_index": idx, "error": str(e)})
                try:
                    driver.switch_to.default_content()
                except Exception:
                    pass
    except Exception as e:
        iframe_results.append({"error": str(e)})

    result = {
        "main_document": main_result,
        "iframes": iframe_results,
    }

    path = os.path.join(OUT_DIR, f"{label}canvases_{ts}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(f"[OK] Canvas info saved: {path}")
    return result


def dump_dom_snapshot(driver, label: str = ""):
    """导出验证码区域的DOM结构"""
    ts = datetime.now().strftime("%H%M%S")

    dom_data = driver.execute_script("""
        function getCaptchaDOM() {
            var result = {containers: [], canvases: [], iframes: []};

            // 所有包含canvas的div容器
            var divs = document.querySelectorAll('div');
            divs.forEach(function(d) {
                var canvases = d.querySelectorAll('canvas');
                if (canvases.length >= 1) {
                    var r = d.getBoundingClientRect();
                    if (r.width > 150 && r.height > 80) {
                        result.containers.push({
                            id: d.id || '',
                            className: (d.className || '').toString().substring(0, 200),
                            tag: d.tagName,
                            rect: {x:Math.round(r.left), y:Math.round(r.top),
                                   w:Math.round(r.width), h:Math.round(r.height)},
                            canvasCount: canvases.length,
                            innerHTML: d.innerHTML.substring(0, 2000)
                        });
                    }
                }
            });

            // 所有canvas信息
            var allCanvas = document.querySelectorAll('canvas');
            allCanvas.forEach(function(c) {
                var r = c.getBoundingClientRect();
                result.canvases.push({
                    id: c.id || '',
                    className: (c.className || '').toString().substring(0, 200),
                    width: c.width,
                    height: c.height,
                    cssWidth: Math.round(r.width),
                    cssHeight: Math.round(r.height),
                    rect: {x:Math.round(r.left), y:Math.round(r.top),
                           w:Math.round(r.width), h:Math.round(r.height)},
                    parent: c.parentElement ? (c.parentElement.id || c.parentElement.className || '').toString().substring(0, 150) : ''
                });
            });

            // 所有iframe
            var ifs = document.querySelectorAll('iframe');
            ifs.forEach(function(f) {
                result.iframes.push({
                    id: f.id || '',
                    src: (f.src || '').substring(0, 200),
                    rect: (function() {
                        var r = f.getBoundingClientRect();
                        return {x:Math.round(r.left), y:Math.round(r.top),
                                w:Math.round(r.width), h:Math.round(r.height)};
                    })(),
                    className: (f.className || '').toString().substring(0, 150)
                });
            });

            return JSON.stringify(result);
        }
        return getCaptchaDOM();
    """)

    path = os.path.join(OUT_DIR, f"{label}dom_{ts}.json")
    with open(path, "w", encoding="utf-8") as f:
        try:
            parsed = json.loads(dom_data)
            json.dump(parsed, f, ensure_ascii=False, indent=2)
        except Exception:
            f.write(dom_data)
    print(f"[OK] DOM snapshot saved: {path}")


def save_screenshots(driver, label: str = ""):
    """保存整页截图和验证码区域截图"""
    ts = datetime.now().strftime("%H%M%S")

    # 整页截图
    try:
        png = driver.get_screenshot_as_png()
        path = os.path.join(OUT_DIR, f"{label}fullpage_{ts}.png")
        with open(path, "wb") as f:
            f.write(png)
        print(f"[OK] Full page screenshot: {path}")
    except Exception as e:
        print(f"[FAIL] Full screenshot error: {e}")

    # 验证码区域截图（如果有canvas）
    try:
        captcha_b64 = driver.execute_script("""
            var canvases = document.querySelectorAll('canvas');
            var minX=Infinity, minY=Infinity, maxX=-Infinity, maxY=-Infinity;
            canvases.forEach(function(c) {
                var r = c.getBoundingClientRect();
                if (r.width < 10 || r.height < 10) return;
                minX = Math.min(minX, r.left);
                minY = Math.min(minY, r.top);
                maxX = Math.max(maxX, r.left + r.width);
                maxY = Math.max(maxY, r.top + r.height);
            });
            if (minX < Infinity) {
                // 扩大40px边距
                minX = Math.max(0, minX - 40);
                minY = Math.max(0, minY - 40);
                maxX = Math.min(window.innerWidth, maxX + 40);
                maxY = Math.min(window.innerHeight, maxY + 80);
                // 截可见区域
                var c = document.createElement('canvas');
                c.width = maxX - minX;
                c.height = maxY - minY;
                // 不能用drawWindow，用html2canvas替代方案：直接返回canvas包围盒
                return JSON.stringify({x:minX, y:minY, w:maxX-minX, h:maxY-minY});
            }
            return '';
        """)
        if captcha_b64:
            rect = json.loads(captcha_b64)
            print(f"[INFO] Captcha bounding box: x={rect['x']}, y={rect['y']}, "
                  f"{rect['w']}x{rect['h']}")
    except Exception as e:
        print(f"[INFO] Captcha area detection: {e}")

    # 逐个canvas导出为独立PNG
    try:
        for idx in range(10):
            has = driver.execute_script(f"""
                var cs = document.querySelectorAll('canvas');
                return {idx} < cs.length;
            """)
            if not has:
                break
            b64 = driver.execute_script(f"""
                var c = document.querySelectorAll('canvas')[{idx}];
                try {{ return c.toDataURL('image/png'); }} catch(e) {{ return ''; }}
            """)
            if b64:
                b64_clean = b64.split(",")[-1] if "," in b64 else b64
                path = os.path.join(OUT_DIR, f"{label}canvas_{idx}_{ts}.png")
                with open(path, "wb") as f:
                    f.write(base64.b64decode(b64_clean))
                print(f"[OK] Canvas #{idx} exported: {path}")
    except Exception as e:
        print(f"[INFO] Canvas export: {e}")


def main():
    if len(sys.argv) < 2:
        print("Usage: python slider_debugger.py <URL>")
        print("Example: python slider_debugger.py https://example.com/jobs")
        print()
        print("This will open Chrome to the URL.")
        print("Manually trigger the captcha (scroll/search/click),")
        print("then press Enter to dump all debug info.")
        return

    url = sys.argv[1]
    if not url.startswith("http"):
        url = "https://" + url

    print("=" * 60)
    print("Slider Captcha Debugger")
    print("=" * 60)
    print(f"Target: {url}")
    print(f"Output: {OUT_DIR}")
    print()
    print("Launching browser...")

    opts = Options()
    opts.binary_location = CHROME_PATH
    opts.add_argument("--window-size=1920,1080")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_argument("--disable-infobars")
    opts.add_argument(f"user-agent={get_random_ua()}")

    service = Service(executable_path=CHROMEDRIVER_PATH)
    service.log_output = None

    driver = webdriver.Chrome(service=service, options=opts)
    driver.get("about:blank")

    # 注入反检测（复用白泽）
    try:
        from baize import baize
        baize.start_all_defense(driver)
    except Exception:
        pass

    driver.get(url)
    time.sleep(3)

    print("\n[READY] Browser is open.")
    print("  -> Navigate to trigger the captcha (click, scroll, search...).")
    input("  -> Press Enter when captcha is visible on screen...")

    label = input("  -> Label for this dump (optional): ").strip()
    if label:
        label = label + "_"

    print("\n[Dumping...]")
    dump_canvas_info(driver, label)
    dump_dom_snapshot(driver, label)
    save_screenshots(driver, label)

    print("\n[DONE] All debug files saved to:", OUT_DIR)
    print("Files:")
    for f in sorted(os.listdir(OUT_DIR)):
        fpath = os.path.join(OUT_DIR, f)
        size_kb = os.path.getsize(fpath) / 1024
        print(f"  {f} ({size_kb:.1f} KB)")

    input("\nPress Enter to close browser...")
    driver.quit()


if __name__ == "__main__":
    main()
