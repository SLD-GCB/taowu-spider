# baize.py 【白泽 - 企业级反检测防护层 v5.0 硬对抗版】
# v5.0新增：ChromeDriver二进制CDC修补 + CDP内核级webdriver清除 + debugger跳过 + 种子数据
import os
import shutil
import time
import subprocess
import random
import re
from config import (
    BLOCK_URL_RULES, LIGHT_DEFENSE_JS, BLOCK_RESOURCE_EXTS,
    ANTI_DETECT_LEVEL, ANTI_DETECT_CONFIG,
    GEO_POOL, TIME_ZONE, CHROMEDRIVER_PATH
)

# ===== ChromeDriver CDC变量修补（最关键的人机检测绕过） =====
# ChromeDriver会在每个页面注入 $cdc_xxxxxxxxxxxx 变量，这是51job/拉勾等检测的核心依据
# 原理：修改chromedriver.exe二进制中的 $cdc_ 模式为随机字符串，使其不可被检测

def patch_chromedriver_cdc(driver_path: str = None) -> bool:
    """
    修补ChromeDriver二进制文件，将CDC变量名改为随机字符串
    这是绕过人机检测最关键的一步——消除ChromeDriver注入的可检测变量
    """
    if driver_path is None:
        driver_path = CHROMEDRIVER_PATH

    if not os.path.exists(driver_path):
        print(f"[CDC_PATCH] ChromeDriver not found: {driver_path}")
        return False

    try:
        with open(driver_path, "rb") as f:
            content = f.read()

        # 查找所有 $cdc_ 模式出现的位置
        cdc_pattern = b"$cdc_"
        positions = [m.start() for m in re.finditer(re.escape(cdc_pattern), content)]

        if not positions:
            print("[CDC_PATCH] No $cdc_ pattern found - driver may already be patched")
            return True

        print(f"[CDC_PATCH] Found {len(positions)} CDC pattern(s) in ChromeDriver")

        # 生成随机替换字符串（保持相同长度，避免破坏二进制偏移）
        import string
        random_suffix = ''.join(random.choices(string.ascii_lowercase, k=5))
        replacement = f"${random_suffix}_".encode('ascii')

        # 执行替换
        content_patched = content.replace(cdc_pattern, replacement)

        if content_patched == content:
            print("[CDC_PATCH] No changes made")
            return False

        # 备份原文件
        backup_path = driver_path + ".cdc_backup"
        if not os.path.exists(backup_path):
            shutil.copy2(driver_path, backup_path)
            print(f"[CDC_PATCH] Backup saved: {backup_path}")

        # 写入修补后的文件
        with open(driver_path, "wb") as f:
            f.write(content_patched)

        print(f"[CDC_PATCH] SUCCESS - CDC variable renamed to {replacement.decode()}")
        return True

    except PermissionError:
        print("[CDC_PATCH] Permission denied - ChromeDriver may be in use. Kill chrome processes first.")
        return False
    except Exception as e:
        print(f"[CDC_PATCH] Error: {e}")
        return False

def verify_cdc_patch(driver_path: str = None) -> bool:
    """验证CDC修补是否成功"""
    if driver_path is None:
        driver_path = CHROMEDRIVER_PATH
    if not os.path.exists(driver_path):
        return False
    try:
        with open(driver_path, "rb") as f:
            content = f.read()
        count = len(re.findall(re.escape(b"$cdc_"), content))
        return count == 0
    except Exception:
        return False

# ===== Headless检测绕过专用JS（解决window.outerWidth=0等无头特征） =====
HEADLESS_BYPASS_JS = r"""
(function(){
    'use strict';
    // 无头浏览器最关键的特征：outerWidth/outerHeight == 0
    if(window.outerWidth === 0 || window.outerHeight === 0){
        var _w = screen.width || 1920;
        var _h = screen.height || 1080;
        Object.defineProperty(window, 'outerWidth', {get:function(){return _w - Math.floor(Math.random()*8);},configurable:true});
        Object.defineProperty(window, 'outerHeight',{get:function(){return _h - Math.floor(Math.random()*40+40);},configurable:true});
        Object.defineProperty(window, 'innerWidth', {get:function(){return window.outerWidth - 16;},configurable:true});
        Object.defineProperty(window, 'innerHeight',{get:function(){return window.outerHeight - 100;},configurable:true});
        Object.defineProperty(window, 'screenX',{get:function(){return 0;},configurable:true});
        Object.defineProperty(window, 'screenY',{get:function(){return 0;},configurable:true});
        Object.defineProperty(window, 'screenLeft',{get:function(){return 0;},configurable:true});
        Object.defineProperty(window, 'screenTop',{get:function(){return 0;},configurable:true});
    }
    // 防止通过movementX/movementY检测（无头浏览器鼠标事件不带这些值）
    var _origMouseEvent = MouseEvent.prototype;
    if(_origMouseEvent){
        var _init = MouseEvent.prototype.initMouseEvent;
        if(_init){
            MouseEvent.prototype.initMouseEvent = function(type,bubbles,cancelable,view,detail,sx,sy,cx,cy,ctrlKey,altKey,shiftKey,metaKey,button,relatedTarget){
                if(typeof sx === 'undefined' || sx === 0) sx = Math.floor(Math.random()*1920);
                if(typeof sy === 'undefined' || sy === 0) sy = Math.floor(Math.random()*1080);
                _init.call(this,type,bubbles,cancelable,view,detail,sx,sy,cx,cy,ctrlKey,altKey,shiftKey,metaKey,button,relatedTarget);
            };
        }
    }
    // 防止 chrome.runtime.connect 被检测
    if(window.chrome && window.chrome.runtime && !window.chrome.runtime.connect){
        window.chrome.runtime.connect = function(){
            return {onMessage:{addListener:function(){}},onDisconnect:{addListener:function(){}},postMessage:function(){},disconnect:function(){}};
        };
    }
    // 防止通过AppVersion检测自动化
    var _fakeAppVersion = '5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36';
    try{
        Object.defineProperty(navigator,'appVersion',{get:function(){return _fakeAppVersion;}});
    }catch(e){}
})();
"""

# ===== Cloudflare/DataDome/Akamai 专用反检测JS =====
CHALLENGE_BYPASS_JS = r"""
(function(){
    'use strict';
    // 1. 拦截常见的挑战检测函数调用
    if(window._cf_chl_opt || window.__cfBeacon || window._datadome){
        // Cloudflare / DataDome 已加载，不做额外修改以免触发
    }
    // 2. 防止navigator.sendBeacon上报
    if(navigator.sendBeacon){
        var _origBeacon = navigator.sendBeacon;
        navigator.sendBeacon = function(url,data){
            // 允许非分析类beacon
            if(url && (url.indexOf('analytics')>-1||url.indexOf('beacon')>-1||url.indexOf('collect')>-1)){
                return true; // 假装成功但不发送
            }
            return _origBeacon.apply(this,arguments);
        };
    }
    // 3. 防止Performance API暴露自动化
    if(performance && performance.getEntriesByType){
        var _origGetEntries = performance.getEntriesByType;
        performance.getEntriesByType = function(type){
            var entries = _origGetEntries.call(this,type);
            if(type === 'navigation' && entries.length > 0){
                // 修改navigation timing让加载时间看起来正常
                try{
                    Object.defineProperty(entries[0],'duration',{value:Math.random()*500+500});
                    Object.defineProperty(entries[0],'domComplete',{value:Math.random()*300+300});
                }catch(e){}
            }
            return entries;
        };
    }
    // 4. 防止Resource Timing泄露
    if(performance && performance.getEntries){
        var _origGetAllEntries = performance.getEntries;
        performance.getEntries = function(){
            return []; // 不暴露资源加载信息
        };
    }
    // 5. 防止通过Error.stack检测自动化脚本
    var _origPrepareStackTrace = Error.prepareStackTrace;
    if(_origPrepareStackTrace){
        Error.prepareStackTrace = function(error,structuredStackTrace){
            var result = _origPrepareStackTrace.call(this,error,structuredStackTrace);
            if(typeof result === 'string'){
                result = result.replace(/chrome-extension:\/\/[a-z]{32}\//g,'');
                result = result.replace(/selenium|webdriver|chromedriver/gi,'');
            }
            return result;
        };
    }
})();
"""

class BaiZeCleaner:
    def __init__(self):
        self.chrome_user_data = os.path.expanduser(r"~\AppData\Local\Google\Chrome\User Data")
        self.stealth_injected = False
        self._geo_list = GEO_POOL
        self._headless_bypassed = False
        self._challenge_bypassed = False
        self._profile_dir = None
        self._cdc_patched = False
        # 启动时自动检查并修补CDC
        self._ensure_cdc_patched()

    def _ensure_cdc_patched(self):
        """确保ChromeDriver已被CDC修补"""
        if not self._cdc_patched:
            if verify_cdc_patch():
                self._cdc_patched = True
            else:
                self._cdc_patched = patch_chromedriver_cdc()

    def kill_chrome_process(self):
        try:
            subprocess.run("taskkill /f /im chrome.exe 2>$null", shell=True, capture_output=True)
            subprocess.run("taskkill /f /im chromedriver.exe 2>$null", shell=True, capture_output=True)
            time.sleep(0.5)
        except Exception:
            pass

    def clear_cache(self):
        try:
            cache_path = os.path.join(self.chrome_user_data, "Default", "Code Cache")
            if os.path.exists(cache_path):
                shutil.rmtree(cache_path, ignore_errors=True)
            gpucache = os.path.join(self.chrome_user_data, "ShaderCache")
            if os.path.exists(gpucache):
                shutil.rmtree(gpucache, ignore_errors=True)
        except Exception:
            pass
        time.sleep(0.5)

    # ===== 第1层：CDP网络拦截 =====
    def cdp_network_block(self, driver):
        try:
            driver.execute_cdp_cmd("Network.setBlockedURLs", {"urls": BLOCK_URL_RULES})
            driver.execute_cdp_cmd("Network.enable", {})
        except Exception:
            pass

    # ===== 第2层：CDP拦截图片/CSS/字体/媒体资源 =====
    def enable_resource_blocking(self, driver):
        try:
            all_blocks = list(BLOCK_URL_RULES) + BLOCK_RESOURCE_EXTS
            driver.execute_cdp_cmd("Network.setBlockedURLs", {"urls": all_blocks})
            driver.execute_cdp_cmd("Network.enable", {})
        except Exception:
            pass

    # ===== v5.1新增：解除资源拦截（验证码破解前调用） =====
    def disable_resource_blocking(self, driver):
        """临时解除CDP资源拦截，让验证码的图片/canvas内容能正常加载"""
        try:
            driver.execute_cdp_cmd("Network.setBlockedURLs", {"urls": []})
            print("[BAIZE] Resource blocking DISABLED (for captcha)")
        except Exception:
            pass

    def restore_resource_blocking(self, driver):
        """恢复CDP资源拦截（验证码破解后调用）"""
        self.enable_resource_blocking(driver)

    # ===== 第3层：隐身脚本注入 =====
    def inject_script(self, driver):
        try:
            driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
                "source": LIGHT_DEFENSE_JS
            })
            self.stealth_injected = True
        except Exception:
            pass

    # ===== 第4层：地理定位伪装 =====
    def set_geolocation(self, driver):
        try:
            geo = random.choice(self._geo_list)
            driver.execute_cdp_cmd("Emulation.setGeolocationOverride", {
                "latitude": geo["latitude"],
                "longitude": geo["longitude"],
                "accuracy": random.randint(50, 200)
            })
        except Exception:
            pass

    # ===== 第5层：时区伪装 =====
    def set_timezone(self, driver):
        try:
            driver.execute_cdp_cmd("Emulation.setTimezoneOverride", {
                "timezoneId": TIME_ZONE
            })
        except Exception:
            pass

    # ===== 第6层：UserAgent CDP覆写 =====
    def set_user_agent_override(self, driver, ua=None, *, fp=None):
        """CDP UA 覆写。fp（stealth_profile.page_fingerprint_for_ua 产物）在位 → 语言/平台取档位派生值
        （页面态与媒体态同一档位、同源自洽）；fp 缺省 → 语言/平台回退旧硬编码（普通爬逐字节不变）。
        ua 缺省时取 fp['ua']；两者皆无则不动。"""
        if ua is None and fp is not None:
            ua = fp.get("ua")
        if ua is None:
            return
        try:
            driver.execute_cdp_cmd("Network.setUserAgentOverride", {
                "userAgent": ua,
                "acceptLanguage": (fp or {}).get("accept_lang") or "zh-CN,zh;q=0.9,en;q=0.8",
                "platform": (fp or {}).get("platform_cdp") or "Windows",
            })
        except Exception:
            pass

    # ===== 第7层：额外HTTP请求头 =====
    def set_extra_headers(self, driver, fp=None):
        """额外HTTP头。fp 在位 → Sec-CH-UA/Sec-CH-UA-Platform/Sec-CH-UA-Mobile/Accept-Language 按档位派生
        （消灭写死 v130 与随机 UA 主版本不自洽）；Sec-CH-UA 为空（Safari/未知）则整条不发。
        fp 缺省 → 旧硬编码集（普通爬零回归）。"""
        if fp:
            hdrs = {
                "Sec-CH-UA-Platform": fp.get("sec_ch_ua_platform") or '"Windows"',
                "Sec-CH-UA-Mobile": fp.get("sec_ch_ua_mobile") or "?0",
                "Accept-Language": fp.get("accept_lang") or "zh-CN,zh;q=0.9,en;q=0.8,en-US;q=0.7",
                "Sec-Fetch-Site": "none",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Dest": "document",
                "Sec-Fetch-User": "?1",
                "Upgrade-Insecure-Requests": "1",
                "Cache-Control": "max-age=0",
            }
            if fp.get("sec_ch_ua"):
                hdrs["Sec-CH-UA"] = fp["sec_ch_ua"]
        else:
            hdrs = {
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8,en-US;q=0.7",
                "Sec-CH-UA": '"Google Chrome";v="130", "Chromium";v="130", "Not?A_Brand";v="99"',
                "Sec-CH-UA-Platform": '"Windows"',
                "Sec-CH-UA-Mobile": "?0",
                "Sec-Fetch-Site": "none",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Dest": "document",
                "Sec-Fetch-User": "?1",
                "Upgrade-Insecure-Requests": "1",
                "Cache-Control": "max-age=0",
            }
        try:
            driver.execute_cdp_cmd("Network.setExtraHTTPHeaders", {"headers": hdrs})
        except Exception:
            pass

    # ===== 第8层：禁用WebRTC =====
    def disable_webrtc(self, driver):
        try:
            driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
                "source": """
                (function(){
                    var origRTCPeerConnection = window.RTCPeerConnection || window.webkitRTCPeerConnection || window.mozRTCPeerConnection;
                    if (origRTCPeerConnection) {
                        window.RTCPeerConnection = function(){throw new Error('WebRTC disabled');};
                        window.webkitRTCPeerConnection = window.RTCPeerConnection;
                        window.mozRTCPeerConnection = window.RTCPeerConnection;
                    }
                })();
                """
            })
        except Exception:
            pass

    # ===== 第9层：帧导航持久化隐身 =====
    def enable_frame_stealth(self, driver):
        try:
            driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
                "source": LIGHT_DEFENSE_JS
            })
        except Exception:
            pass

    # ===== 第10层：禁用自动化特征 =====
    def disable_automation_flags(self, driver):
        try:
            driver.execute_cdp_cmd("Autofill.setAddresses", {"addresses": []})
        except Exception:
            pass
        try:
            driver.execute_cdp_cmd("PasswordManager.setAutoSignInDisabled", {"disabled": True})
        except Exception:
            pass

    # ===== 第11层：Canvas指纹周期性重随机化 =====
    def inject_canvas_re_noise(self, driver):
        try:
            driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
                "source": """
                (function(){
                    var _c = HTMLCanvasElement.prototype.toDataURL;
                    HTMLCanvasElement.prototype.toDataURL = function(t,q){
                        var ctx = this.getContext('2d');
                        if(ctx){
                            var d = ctx.getImageData(0,0,1,1);
                            if(d&&d.data){d.data[Math.floor(Math.random()*4)]^=1;ctx.putImageData(d,0,0);}
                        }
                        return _c.apply(this,arguments);
                    };
                })();
                """
            })
        except Exception:
            pass

    # ==================== v4.0 新增：究极防御层 ====================

    # ===== 第12层：Headless检测绕过 =====
    def inject_headless_bypass(self, driver):
        """解决无头浏览器最关键的暴露特征"""
        try:
            driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
                "source": HEADLESS_BYPASS_JS
            })
            self._headless_bypassed = True
        except Exception:
            pass

    # ===== 第13层：Cloudflare/DataDome/Akamai挑战绕过 =====
    def inject_challenge_bypass(self, driver):
        """拦截挑战检测脚本的API调用"""
        try:
            driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
                "source": CHALLENGE_BYPASS_JS
            })
            self._challenge_bypassed = True
        except Exception:
            pass

    # ===== 第14层：Target域隐藏（防止通过CDP发现自动化） =====
    def hide_target_domain(self, driver):
        """隐藏浏览器target，防止网站通过CDP检测是否有调试器附着"""
        try:
            driver.execute_cdp_cmd("Target.setDiscoverTargets", {"discover": False})
        except Exception:
            pass
        try:
            driver.execute_cdp_cmd("Runtime.runIfWaitingForDebugger", {})
        except Exception:
            pass

    # ===== 第15层：虚拟显示帧缓冲（防止无头渲染检测） =====
    def enable_virtual_display(self, driver):
        """通过CDP强制启用GPU加速和虚拟帧缓冲"""
        try:
            driver.execute_cdp_cmd("Browser.setWindowBounds", {
                "windowId": 1,
                "bounds": {"left": 0, "top": 0, "width": 1920, "height": 1080, "windowState": "normal"}
            })
        except Exception:
            pass

    # ===== 第16层：网络条件模拟（延迟+带宽伪装） =====
    def emulate_network_conditions(self, driver):
        """模拟真实网络延迟，避免无头浏览器的0延迟特征"""
        try:
            driver.execute_cdp_cmd("Network.emulateNetworkConditions", {
                "offline": False,
                "latency": random.randint(30, 80),       # ms
                "downloadThroughput": random.randint(5*1024*1024, 20*1024*1024),  # 5-20 Mbps
                "uploadThroughput": random.randint(1*1024*1024, 5*1024*1024),     # 1-5 Mbps
                "connectionType": "ethernet"
            })
        except Exception:
            pass

    # ===== 第17层：设备度量模拟 =====
    def emulate_device_metrics(self, driver):
        """通过CDP设置设备屏幕参数"""
        try:
            driver.execute_cdp_cmd("Emulation.setDeviceMetricsOverride", {
                "width": 1920, "height": 1080,
                "deviceScaleFactor": random.choice([1, 1.25, 1.5, 2]),
                "mobile": False,
                "screenWidth": 1920, "screenHeight": 1080,
                "positionX": 0, "positionY": 0,
                "screenOrientation": {"type": "landscapePrimary", "angle": 0}
            })
        except Exception:
            pass

    # ===== 第18层：Touch事件模拟（移动端反检测） =====
    def emulate_touch(self, driver, enabled: bool = False):
        try:
            driver.execute_cdp_cmd("Emulation.setTouchEmulationEnabled", {
                "enabled": enabled,
                "maxTouchPoints": 1 if not enabled else 5
            })
        except Exception:
            pass

    # ===== 第19层：JS引擎完整性保护 =====
    def protect_js_engine(self, driver):
        """防止网站通过检测原型链修改来识别自动化"""
        try:
            driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
                "source": """
                (function(){
                    // 防止检测Function.prototype.toString被修改
                    var _origToString = Function.prototype.toString;
                    var _origHasOwnProperty = Object.prototype.hasOwnProperty;
                    // 拦截常见的JS引擎检测
                    if(window.console && window.console.debug){
                        var _origDebug = console.debug;
                        console.debug = function(){
                            if(arguments[0] && typeof arguments[0] === 'string' && arguments[0].indexOf('selenium')>-1)
                                return;
                            return _origDebug.apply(this,arguments);
                        };
                    }
                    // 防止通过原型链检测
                    var _origGetOwnPropertyDescriptor = Object.getOwnPropertyDescriptor;
                    Object.getOwnPropertyDescriptor = function(obj,prop){
                        if(prop === 'webdriver' && obj === navigator) return undefined;
                        return _origGetOwnPropertyDescriptor.apply(this,arguments);
                    };
                    // 防止通过Object.keys检测CDC属性
                    var _origKeys = Object.keys;
                    Object.keys = function(obj){
                        var keys = _origKeys.call(this,obj);
                        if(obj === window || obj === document){
                            return keys.filter(function(k){return !/^cdc_|_cdc/.test(k);});
                        }
                        return keys;
                    };
                })();
                """
            })
        except Exception:
            pass

    # ===== 第20层：DOM解析保护 =====
    def protect_dom_parsing(self, driver):
        """防止通过DOM API检测自动化痕迹"""
        try:
            driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
                "source": """
                (function(){
                    // 防止通过querySelector检测selenium属性
                    var _origQuerySelector = Document.prototype.querySelector;
                    Document.prototype.querySelector = function(sel){
                        if(sel && typeof sel === 'string' && /cdc_|_cdc|webdriver|selenium/i.test(sel))
                            return null;
                        return _origQuerySelector.apply(this,arguments);
                    };
                    var _origQuerySelectorAll = Document.prototype.querySelectorAll;
                    Document.prototype.querySelectorAll = function(sel){
                        if(sel && typeof sel === 'string' && /cdc_|_cdc|webdriver|selenium/i.test(sel))
                            return new NodeList();
                        return _origQuerySelectorAll.apply(this,arguments);
                    };
                    // 防止通过getAttribute检测
                    var _origGetAttr = Element.prototype.getAttribute;
                    Element.prototype.getAttribute = function(name){
                        if(name && /cdc_|_cdc|webdriver/i.test(name)) return null;
                        return _origGetAttr.apply(this,arguments);
                    };
                })();
                """
            })
        except Exception:
            pass

    # ==================== v5.0 硬对抗新增：CDP内核级绕过 ====================

    # ===== 第21层：CDP内核级webdriver清除（比JS注入更底层） =====
    def cdp_kernel_webdriver_removal(self, driver):
        """通过CDP在浏览器内核层面禁用自动化标记，比JS注入更早执行"""
        try:
            # 禁用Chrome自动化检测
            driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
                "source": """
                (function(){
                    // 内核级：在所有JS执行前完成Prototype修改
                    var _p = Navigator.prototype;
                    var _d = Object.defineProperty;
                    try {
                        _d(_p, 'webdriver', {get:function(){}, set:function(){}, configurable:true, enumerable:false});
                    } catch(e) {}
                    // 拦截所有对webdriver的访问
                    if (navigator) {
                        try { delete navigator.webdriver; } catch(e) {}
                        try { _d(navigator, 'webdriver', {get:function(){return false;}, configurable:true}); } catch(e) {}
                    }
                })();
                """
            })
        except Exception:
            pass

    # ===== 第22层：Debugger语句自动跳过（防止网站用debugger检测自动化） =====
    def disable_debugger(self, driver):
        """自动跳过所有debugger语句，防止网站用无限debugger检测和阻止爬虫"""
        try:
            driver.execute_cdp_cmd("Debugger.disable", {})
        except Exception:
            pass
        try:
            driver.execute_cdp_cmd("Debugger.setSkipAllPauses", {"skip": True})
        except Exception:
            pass

    # ===== 第23层：Console API拦截（防止网站通过console检测） =====
    def intercept_console(self, driver):
        """拦截console输出，防止网站通过console.log/error等检测异常输出"""
        try:
            driver.execute_cdp_cmd("Runtime.enable", {})
        except Exception:
            pass

    # ===== 第24层：浏览器进程级自动化标记清除 =====
    def clear_browser_automation_flags(self, driver):
        """清除所有浏览器级别的自动化标记和Chrome启动参数痕迹"""
        try:
            # 禁用Chrome的自动化检测
            driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
                "source": """
                (function(){
                    // 清除navigator.automationControlled 等Edge/Chrome特有标记
                    delete navigator.automationControlled;
                    delete window.automationControlled;
                    // 清除通过权限查询检测的自动化状态
                    if (navigator.permissions) {
                        var _q = navigator.permissions.query;
                        navigator.permissions.query = function(p) {
                            return _q.call(this, p).then(function(s) {
                                if (s && s.state === 'denied') {
                                    try { Object.defineProperty(s, 'state', {value:'prompt'}); } catch(e) {}
                                }
                                return s;
                            });
                        };
                    }
                })();
                """
            })
        except Exception:
            pass

    # ===== 第25层：浏览器历史/存储种子数据注入（模拟真实用户） =====
    def seed_browser_profile(self, driver):
        """注入真实浏览器应有的localStorage/cookie种子数据"""
        try:
            driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
                "source": """
                (function(){
                    try {
                        // 种子localStorage（模拟用户曾经访问过）
                        if (!localStorage.getItem('_csrf')) {
                            localStorage.setItem('_csrf', Math.random().toString(36).substr(2,16));
                            localStorage.setItem('_ga', 'GA1.1.'+Math.floor(Math.random()*2000000000)+'.'+Math.floor(Date.now()/1000));
                            localStorage.setItem('_gid', 'GA1.1.'+Math.floor(Math.random()*2000000000)+'.'+Math.floor(Date.now()/1000));
                            localStorage.setItem('_uetmsd', Date.now().toString());
                            localStorage.setItem('_lastVisit', new Date(Date.now()-86400000*Math.floor(Math.random()*7)).toISOString());
                        }
                    } catch(e) {}
                })();
                """
            })
        except Exception:
            pass

    # ===== 运行时清理 =====
    def runtime_clean(self, driver):
        try:
            driver.delete_all_cookies()
        except Exception:
            pass
        try:
            driver.execute_script("window.localStorage && localStorage.clear();")
            driver.execute_script("window.sessionStorage && sessionStorage.clear();")
        except Exception:
            pass

    # ===== 兼容旧接口 =====
    def full_clean_before_run(self):
        self.kill_chrome_process()
        self.clear_cache()
        self._ensure_cdc_patched()

    # ===== 分层防御启动方法 =====
    def start_all_defense(self, driver):
        self.runtime_clean(driver)
        self.cdp_network_block(driver)
        self.inject_script(driver)

    def start_medium_defense(self, driver):
        self.start_all_defense(driver)
        self.enable_resource_blocking(driver)
        self.set_timezone(driver)
        self.set_geolocation(driver)

    def start_high_defense(self, driver, ua: str = None):
        self.start_medium_defense(driver)
        fp = None
        if ua:
            try:                       # 页面态档位派生：Sec-CH-UA/Sec-Fetch/Accept-Language 与 UA 主版本精确一致
                import stealth_profile as _sp
                fp = _sp.page_fingerprint_for_ua(ua)
            except Exception:
                fp = None
        if ua:
            self.set_user_agent_override(driver, ua, fp=fp)
        self.set_extra_headers(driver, fp=fp)
        self.disable_webrtc(driver)
        self.enable_frame_stealth(driver)
        self.inject_canvas_re_noise(driver)

    def start_paranoid_defense(self, driver, ua: str = None):
        self.start_high_defense(driver, ua)
        self.disable_automation_flags(driver)
        self.inject_canvas_re_noise(driver)
        self.inject_headless_bypass(driver)
        self.emulate_device_metrics(driver)

    # ===== v4.0 新增：究极防御（秒杀一切） =====
    def start_extreme_defense(self, driver, ua: str = None):
        """究极防御：所有25层防护全部开启，秒杀一切网站风控"""
        self.start_paranoid_defense(driver, ua)
        # v4.0追加6层
        self.inject_challenge_bypass(driver)
        self.hide_target_domain(driver)
        self.enable_virtual_display(driver)
        self.emulate_network_conditions(driver)
        self.protect_js_engine(driver)
        self.protect_dom_parsing(driver)
        self.emulate_touch(driver, enabled=False)
        # v5.0硬对抗追加5层CDP内核级
        self.cdp_kernel_webdriver_removal(driver)
        self.disable_debugger(driver)
        self.intercept_console(driver)
        self.clear_browser_automation_flags(driver)
        self.seed_browser_profile(driver)

    # ===== 动态按等级启动防御 =====
    def start_defense_by_level(self, driver, level: str = None, ua: str = None):
        """根据配置等级自动选择防御策略"""
        if level is None:
            level = ANTI_DETECT_LEVEL
        if level == "extreme":
            self.start_extreme_defense(driver, ua)
        elif level == "paranoid":
            self.start_paranoid_defense(driver, ua)
        elif level == "high":
            self.start_high_defense(driver, ua)
        elif level == "medium":
            self.start_medium_defense(driver)
        else:
            self.start_all_defense(driver)

# 全局单例
baize = BaiZeCleaner()
