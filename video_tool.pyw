import os
import sys
import re
import threading
import subprocess
import tkinter as tk
import tkinter.font as tkfont
from tkinter import ttk, filedialog, messagebox
import zipfile
import math
import shutil
import json
import pandas as pd

# === 核心修改：动态获取外部 ffmpeg 文件夹路径 ===
def get_bin_path(filename):
    """
    优先检查环境变量，其次探测本地打包目录，完美兼容 Windows 与 macOS 应用程序包 (.app)。
    """
    if sys.platform.startswith('win') and not filename.endswith('.exe'):
        filename += '.exe'
        
    env_path = shutil.which(filename)
    if env_path:
        return env_path
        
    if getattr(sys, 'frozen', False):
        if sys.platform == 'darwin' and sys.executable.endswith('MacOS/video_tool'):
            # 兼容 macOS 的 .app 应用程序包 (向上退 4 级回到 .app 同级目录)
            base_path = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(sys.executable))))
        else:
            # 兼容 Windows 用户直接下载解压的情况 (exe 与 ffmpeg 文件夹同级)
            base_path = os.path.dirname(sys.executable)
    else:
        # 源码运行
        base_path = os.path.dirname(os.path.abspath(__file__))
    
    return os.path.join(base_path, "ffmpeg", filename)

# === 新增：智能检测系统 GPU 并分配最佳编码器 ===
def auto_detect_gpu():
    """
    跨平台智能探测系统显卡。
    双重引擎探测：优先使用 PowerShell（适配最新 Win11），备用 wmic（兼容老系统）。
    Windows 优先逆序匹配（优先识别独显 GPU 1，其次核显 GPU 0）；
    macOS 直接返回 Apple VideoToolbox。
    """
    if sys.platform == 'darwin':
        return "Apple (VideoToolbox)"
    elif sys.platform.startswith('win'):
        gpus = []
        # 引擎 1：现代 Windows 11 首选 PowerShell 探测
        try:
            ps_cmd = 'Get-CimInstance Win32_VideoController | Select-Object -ExpandProperty Name'
            result = subprocess.check_output(
                ["powershell", "-NoProfile", "-Command", ps_cmd], 
                creationflags=subprocess.CREATE_NO_WINDOW, 
                text=True, errors='ignore'
            )
            gpus = [line.strip() for line in result.split('\n') if line.strip()]
        except Exception:
            pass
            
        # 引擎 2：如果 PowerShell 失败，回退到经典 wmic 探测 (兼容 Win10/Win7)
        if not gpus:
            try:
                result = subprocess.check_output(
                    ["wmic", "path", "win32_VideoController", "get", "name"],
                    creationflags=subprocess.CREATE_NO_WINDOW,
                    text=True, errors='ignore'
                )
                gpus = [line.strip() for line in result.split('\n') if line.strip() and line.strip().lower() != 'name']
            except Exception:
                pass

        # 逆序遍历显卡列表（倒序检查，优先匹配 GPU 1 独显）
        for gpu in reversed(gpus):
            gpu_lower = gpu.lower()
            if "nvidia" in gpu_lower:
                return "NVIDIA (NVENC)"
            elif "amd" in gpu_lower or "radeon" in gpu_lower:
                return "AMD (AMF)"
        
        # 如果没有独立显卡，再统一检查核显
        for gpu in reversed(gpus):
            if "intel" in gpu.lower():
                return "Intel (QSV)"
                
    return "CPU (H.264)" # 探测失败或无支持硬件时的兜底方案

class FFmpegUltimateTool:
    def __init__(self, root):
        self.root = root
        self.root.title("视频批量处理工具")
        # 增加窗口宽度与高度以完美容纳加宽的下拉框和单选框
        self.root.geometry("845x630")
        self.root.resizable(True, True)
        self.root.minsize(600, 600)

        style = ttk.Style()
        if "vista" in style.theme_names():
            style.theme_use("vista")
        elif "winnative" in style.theme_names():
            style.theme_use("winnative")

        # === 变量定义 ===
        self.in_dir = tk.StringVar()
        self.out_dir = tk.StringVar()
        
        self.supported_exts = ['.mp4', '.mkv', '.mov', '.avi', '.wmv', '.flv', '.webm', '.ts', '.m4v']
        self.target_fmt_var = tk.StringVar(value="保持原格式")
        self.target_fmt_options = ["保持原格式", "MP4", "MKV", "MOV", "AVI"]

        # 智能获取系统推荐的硬件加速器
        default_encoder = auto_detect_gpu()

        self.codec_var = tk.StringVar(value="H.264") # 新增: 视频编码格式
        self.encoder_var = tk.StringVar(value=default_encoder)
        self.encoder_map = {
            "CPU (H.264)": "libx264",
            "NVIDIA (NVENC)": "h264_nvenc",
            "AMD (AMF)": "h264_amf",
            "Intel (QSV)": "h264_qsv",
            "Apple (VideoToolbox)": "h264_videotoolbox", # 新增: 兼容 Mac M系列芯片
            "纯转封装 (极速)": "copy"
        }

        self.res_mode = tk.IntVar(value=1)
        self.prop_w_en = tk.BooleanVar(value=True) 
        self.prop_w = tk.StringVar(value="1920")
        self.prop_h_en = tk.BooleanVar(value=False)
        self.prop_h = tk.StringVar(value="1080")
        self.exact_w = tk.StringVar(value="1920")
        self.exact_h = tk.StringVar(value="1080")

        self.quality_mode = tk.IntVar(value=2)
        self.bitrate = tk.StringVar(value="3000")
        self.max_bitrate = tk.StringVar(value="3000") # 新增: 批量处理限流上限
        self.crf = tk.StringVar(value="28")
        self.preset = tk.StringVar(value="medium")
        self.copy_audio = tk.BooleanVar(value=True)
        self.faststart = tk.BooleanVar(value=True)
        
        self.fps_var = tk.StringVar(value="保持原始")
        self.fps_options = ["保持原始", "23.976", "24", "25", "29.97", "30", "50", "60"]

        self.threads_var = tk.StringVar(value="自动")
        self.threads_options = ["自动", "1", "2", "4", "8", "16", "32"]

        self.audio_bitrate_var = tk.StringVar(value="192") # 新增: 视频批量处理音频码率

        self.force_sync = tk.BooleanVar(value=False)

        self.output_as_zip = tk.BooleanVar(value=False)
        self.zip_max_var = tk.StringVar(value="50")

        # === 变量定义 (音视频合并专属) ===
        self.m_fmt_var = tk.StringVar(value="保持原格式")
        self.m_fmt_options = ["保持原格式", "MP4", "MKV", "MOV", "AVI"]
        
        # 补全上一版遗漏的核心变量
        self.m_codec_var = tk.StringVar(value="H.264") # 新增: 视频编码格式
        self.m_encoder_var = tk.StringVar(value=default_encoder)
        self.m_quality_mode = tk.IntVar(value=2)
        self.m_bitrate = tk.StringVar(value="3000")
        self.m_max_bitrate = tk.StringVar(value="3000") # 新增: 合并界面限流上限
        self.m_crf = tk.StringVar(value="28")
        self.m_audio_bitrate_var = tk.StringVar(value="320") # 新增: 默认 320kbps 高音质
        
        # === 新增: 外部音频偏移与时间轴拉伸适应 ===
        self.m_audio_offset = tk.StringVar(value="0") # 毫秒 (ms)，正数推后，负数提前
        self.m_force_tempo = tk.BooleanVar(value=False)
        self.m_force_crop = tk.BooleanVar(value=True) # 新增: 强制裁剪音频尾巴对齐视频
        
        self.m_res_mode = tk.IntVar(value=1)
        self.m_prop_w_en = tk.BooleanVar(value=True) 
        self.m_prop_w = tk.StringVar(value="1920")
        self.m_prop_h_en = tk.BooleanVar(value=False)
        self.m_prop_h = tk.StringVar(value="1080")
        self.m_exact_w = tk.StringVar(value="1920")
        self.m_exact_h = tk.StringVar(value="1080")

        self.m_fps_var = tk.StringVar(value="保持原始")
        self.m_preset = tk.StringVar(value="medium")
        self.m_threads_var = tk.StringVar(value="自动")
        
        self.m_output_as_zip = tk.BooleanVar(value=False)
        self.m_zip_max_var = tk.StringVar(value="50")

        self.progress_var = tk.DoubleVar(value=0)
        self.status_text = tk.StringVar(value="等待开始...")
        self.is_processing = False
        self.is_cancelled = False
        self.current_process = None

        self.re_duration = re.compile(r"Duration: (\d{2}):(\d{2}):(\d{2}\.\d+)")
        self.re_time = re.compile(r"time=(\d{2}):(\d{2}):(\d{2}\.\d+)")

        # === 获取组件绝对路径 ===
        self.ffmpeg_bin = get_bin_path("ffmpeg")
        self.ffprobe_bin = get_bin_path("ffprobe")

        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

        self.tab_process = ttk.Frame(self.notebook)
        self.notebook.add(self.tab_process, text=" 视频批量处理 ")
        
        self.tab_stat = ttk.Frame(self.notebook)
        self.notebook.add(self.tab_stat, text=" 视频信息统计 ")

        self.tab_audio = ttk.Frame(self.notebook)
        self.notebook.add(self.tab_audio, text=" 音频提取 ")
        
        self.tab_merge = ttk.Frame(self.notebook)
        self.notebook.add(self.tab_merge, text=" 音视频合并 ")

        self.setup_ui()
        self.update_res_ui()
        self.update_zip_ui() 
        self.setup_stat_ui() 
        self.setup_audio_ui() 
        self.setup_merge_ui()

        # === 变量定义 (视频拆分/合并专属) ===
        self.sm_action = tk.IntVar(value=1) # 1: 拆分, 2: 合并
        
        self.sm_split_in = tk.StringVar()
        self.sm_split_out1 = tk.StringVar()
        self.sm_split_out2 = tk.StringVar()
        self.sm_split_mode = tk.IntVar(value=2) # 1: 时间, 2: 百分比
        self.sm_split_dir = tk.IntVar(value=1)  # 新增: 1为正序，2为倒序
        self.sm_split_time = tk.StringVar(value="00:00:00")
        self.sm_split_pct = tk.StringVar(value="50")
        
        self.sm_merge_in1 = tk.StringVar()
        self.sm_merge_in2 = tk.StringVar()
        self.sm_merge_out = tk.StringVar()
        
        self.sm_keep_audio = tk.BooleanVar(value=True)
        self.sm_copy_stream = tk.BooleanVar(value=True) # 默认选中：只拆分/合并，不重编码
        
        self.sm_fmt_var = tk.StringVar(value="保持原格式")
        self.sm_codec_var = tk.StringVar(value="H.264") # 新增: 视频编码格式
        self.sm_encoder_var = tk.StringVar(value=default_encoder)
        self.sm_quality_mode = tk.IntVar(value=2)
        self.sm_bitrate = tk.StringVar(value="3000")
        self.sm_max_bitrate = tk.StringVar(value="3000") # 新增: 拆分合并限流上限
        self.sm_crf = tk.StringVar(value="28")
        self.sm_audio_bitrate_var = tk.StringVar(value="320") # 新增: 默认 320kbps 高音质
        
        self.sm_res_mode = tk.IntVar(value=1)
        self.sm_prop_w_en = tk.BooleanVar(value=True) 
        self.sm_prop_w = tk.StringVar(value="1920")
        self.sm_prop_h_en = tk.BooleanVar(value=False)
        self.sm_prop_h = tk.StringVar(value="1080")
        self.sm_exact_w = tk.StringVar(value="1920")
        self.sm_exact_h = tk.StringVar(value="1080")

        self.sm_fps_var = tk.StringVar(value="保持原始")
        self.sm_preset = tk.StringVar(value="medium")
        self.sm_threads_var = tk.StringVar(value="自动")

        self.sm_progress_var = tk.DoubleVar(value=0)
        self.sm_status_text = tk.StringVar(value="等待开始...")
        self.is_sm_processing = False

        # === 注册新标签页 ===
        self.tab_split_merge = ttk.Frame(self.notebook)
        self.notebook.add(self.tab_split_merge, text=" 视频拆分/合并 ")
        
        self.setup_split_merge_ui()

    # ================= 原视频处理 UI =================
    def setup_ui(self):
        frame_dir = ttk.Frame(self.tab_process, padding=10)
        frame_dir.pack(fill="x")
        frame_dir.columnconfigure(1, weight=1) 
        
        ttk.Label(frame_dir, text="输入目录:").grid(row=0, column=0, sticky="e")
        ttk.Entry(frame_dir, textvariable=self.in_dir).grid(row=0, column=1, sticky="we", padx=5)
        ttk.Button(frame_dir, text="浏览...", command=lambda: self.browse_dir(self.in_dir)).grid(row=0, column=2)

        ttk.Label(frame_dir, text="输出目录:").grid(row=1, column=0, sticky="e", pady=5)
        ttk.Entry(frame_dir, textvariable=self.out_dir).grid(row=1, column=1, sticky="we", padx=5, pady=5)
        ttk.Button(frame_dir, text="浏览...", command=lambda: self.browse_dir(self.out_dir)).grid(row=1, column=2)

        frame_global = ttk.LabelFrame(self.tab_process, text="核心引擎设置", padding=10)
        frame_global.pack(fill="x", padx=10, pady=5)
        
        ttk.Label(frame_global, text="输出格式:").pack(side="left")
        cb_format = ttk.Combobox(frame_global, textvariable=self.target_fmt_var, values=self.target_fmt_options, width=8, state="readonly")
        cb_format.pack(side="left", padx=(2, 0))

        ttk.Label(frame_global, text=" 视频编码:").pack(side="left", padx=(10, 2))
        cb_vcodec = ttk.Combobox(frame_global, textvariable=self.codec_var, values=["保持原始", "H.264", "H.265"], width=8, state="readonly")
        cb_vcodec.pack(side="left")

        ttk.Label(frame_global, text=" 硬件加速:").pack(side="left", padx=(10, 2))
        cb_encoder = ttk.Combobox(frame_global, textvariable=self.encoder_var, values=list(self.encoder_map.keys()), width=14, state="readonly")
        cb_encoder.pack(side="left")

        frame_settings = ttk.Frame(self.tab_process, padding=10)
        frame_settings.pack(fill="both", expand=True)

        lf_res = ttk.LabelFrame(frame_settings, text="智能分辨率设置", padding=10)
        lf_res.pack(side="left", fill="both", expand=True, padx=(0, 5))

        ttk.Radiobutton(lf_res, text="保持原分辨率", variable=self.res_mode, value=1, command=self.update_res_ui).grid(row=0, column=0, columnspan=2, sticky="w")
        ttk.Radiobutton(lf_res, text="单边等比缩放", variable=self.res_mode, value=2, command=self.update_res_ui).grid(row=1, column=0, columnspan=2, sticky="w", pady=(5,0))
        
        self.cb_prop_w = ttk.Checkbutton(lf_res, text="限制宽(px):", variable=self.prop_w_en, command=self.on_prop_w_check)
        self.cb_prop_w.grid(row=2, column=0, sticky="w", padx=(20, 0))
        self.entry_prop_w = ttk.Entry(lf_res, textvariable=self.prop_w, width=8)
        self.entry_prop_w.grid(row=2, column=1, sticky="w")
        
        self.cb_prop_h = ttk.Checkbutton(lf_res, text="限制高(px):", variable=self.prop_h_en, command=self.on_prop_h_check)
        self.cb_prop_h.grid(row=3, column=0, sticky="w", padx=(20, 0))
        self.entry_prop_h = ttk.Entry(lf_res, textvariable=self.prop_h, width=8)
        self.entry_prop_h.grid(row=3, column=1, sticky="w")

        ttk.Radiobutton(lf_res, text="强制指定宽高", variable=self.res_mode, value=3, command=self.update_res_ui).grid(row=4, column=0, columnspan=2, sticky="w", pady=(5,0))
        ttk.Label(lf_res, text="宽(px):").grid(row=5, column=0, sticky="e")
        self.entry_exact_w = ttk.Entry(lf_res, textvariable=self.exact_w, width=8)
        self.entry_exact_w.grid(row=5, column=1, sticky="w")
        ttk.Label(lf_res, text="高(px):").grid(row=6, column=0, sticky="e")
        self.entry_exact_h = ttk.Entry(lf_res, textvariable=self.exact_h, width=8)
        self.entry_exact_h.grid(row=6, column=1, sticky="w")

        lf_other = ttk.LabelFrame(frame_settings, text="画质与优化", padding=10)
        lf_other.pack(side="right", fill="both", expand=True, padx=(5, 0))

        ttk.Radiobutton(lf_other, text="平均码率(kbps):", variable=self.quality_mode, value=1).grid(row=0, column=0, sticky="w")
        ttk.Entry(lf_other, textvariable=self.bitrate, width=8).grid(row=0, column=1)

        # 新增的智能限流选项
        ttk.Radiobutton(lf_other, text="智能限流(上限kbps):", variable=self.quality_mode, value=4).grid(row=1, column=0, sticky="w", pady=(5,0))
        ttk.Entry(lf_other, textvariable=self.max_bitrate, width=8).grid(row=1, column=1, sticky="w", pady=(5,0))

        # 后面的行号整体 +1
        ttk.Radiobutton(lf_other, text="动态质量(推荐28):", variable=self.quality_mode, value=2).grid(row=2, column=0, sticky="w", pady=(5,0))
        ttk.Entry(lf_other, textvariable=self.crf, width=8).grid(row=2, column=1, sticky="w", pady=(5,0))

        ttk.Radiobutton(lf_other, text="保持原视频流码率", variable=self.quality_mode, value=3).grid(row=3, column=0, columnspan=2, sticky="w", pady=(5,0))

        ttk.Label(lf_other, text="视频帧率(FPS):").grid(row=4, column=0, sticky="w", pady=(10,0))
        cb_fps = ttk.Combobox(lf_other, textvariable=self.fps_var, values=self.fps_options, width=8, state="readonly")
        cb_fps.grid(row=4, column=1, pady=(10,0))

        ttk.Label(lf_other, text="编码预设:").grid(row=5, column=0, sticky="w", pady=(10,0))
        cb_preset = ttk.Combobox(lf_other, textvariable=self.preset, values=["fast", "medium", "slow"], width=8, state="readonly")
        cb_preset.grid(row=5, column=1, pady=(10,0))

        ttk.Label(lf_other, text="线程数:").grid(row=6, column=0, sticky="w", pady=(10,0))
        cb_threads = ttk.Combobox(lf_other, textvariable=self.threads_var, values=self.threads_options, width=8, state="readonly")
        cb_threads.grid(row=6, column=1, pady=(10,0))

        ttk.Label(lf_other, text="音频码率:").grid(row=7, column=0, sticky="w", pady=(10,0))
        cb_audio_br = ttk.Combobox(lf_other, textvariable=self.audio_bitrate_var, values=["保持原始", "128", "192", "256", "320"], width=8)
        cb_audio_br.grid(row=7, column=1, pady=(10,0))

        ttk.Separator(lf_other, orient='horizontal').grid(row=8, column=0, columnspan=2, sticky="we", pady=10)
        ttk.Checkbutton(lf_other, text="保留原音频 (无损极速)", variable=self.copy_audio).grid(row=9, column=0, columnspan=2, sticky="w")
        ttk.Checkbutton(lf_other, text="Web优化 (边下边播)", variable=self.faststart).grid(row=10, column=0, columnspan=2, sticky="w")
        ttk.Checkbutton(lf_other, text="强制音画同步 (防时长微调)", variable=self.force_sync).grid(row=11, column=0, columnspan=2, sticky="w")

        # === 替换为新的底部紧凑操作区 (同行布局) ===
        frame_bottom = ttk.Frame(self.tab_process, padding=(10, 10))
        frame_bottom.pack(fill="x", side="bottom", pady=5)
        frame_bottom.columnconfigure(4, weight=1) # 让进度条自动填充剩余横向空间

        ttk.Checkbutton(frame_bottom, text="打包ZIP分卷", variable=self.output_as_zip, command=self.update_zip_ui).grid(row=0, column=0, sticky="w")
        self.lbl_zip = ttk.Label(frame_bottom, text="上限:")
        self.lbl_zip.grid(row=0, column=1, sticky="w", padx=(5, 0))
        self.entry_zip_max = ttk.Entry(frame_bottom, textvariable=self.zip_max_var, width=5)
        self.entry_zip_max.grid(row=0, column=2, sticky="w", padx=(0, 10))
        self.update_zip_ui() # 初始化组件状态

        self.lbl_status = ttk.Label(frame_bottom, textvariable=self.status_text)
        self.lbl_status.grid(row=0, column=3, sticky="e", padx=(0, 5))

        self.progress_bar = ttk.Progressbar(frame_bottom, orient="horizontal", mode="determinate", variable=self.progress_var)
        self.progress_bar.grid(row=0, column=4, sticky="we", padx=10)

        self.btn_run = ttk.Button(frame_bottom, text="开始处理", width=12, command=self.start_processing)
        self.btn_run.grid(row=0, column=5, padx=5)

        self.btn_stop = ttk.Button(frame_bottom, text="停止处理", width=12, command=self.stop_processing, state="disabled")
        self.btn_stop.grid(row=0, column=6)

    # ================= 信息统计 UI 与逻辑 =================
    def setup_stat_ui(self):
        self.stat_in_dir = tk.StringVar()
        self.stat_out_dir = tk.StringVar()
        self.stat_progress_var = tk.DoubleVar(value=0)
        self.stat_status_text = tk.StringVar(value="等待开始...")
        self.is_stating = False

        frame_dir = ttk.Frame(self.tab_stat, padding=20)
        frame_dir.pack(fill="x")
        frame_dir.columnconfigure(1, weight=1)

        ttk.Label(frame_dir, text="需统计的视频目录:").grid(row=0, column=0, sticky="e", pady=15)
        ttk.Entry(frame_dir, textvariable=self.stat_in_dir).grid(row=0, column=1, sticky="we", padx=5, pady=15)
        ttk.Button(frame_dir, text="浏览...", command=lambda: self.browse_dir(self.stat_in_dir)).grid(row=0, column=2, pady=15)

        ttk.Label(frame_dir, text="表格输出目录:").grid(row=1, column=0, sticky="e", pady=15)
        ttk.Entry(frame_dir, textvariable=self.stat_out_dir).grid(row=1, column=1, sticky="we", padx=5, pady=15)
        ttk.Button(frame_dir, text="浏览...", command=lambda: self.browse_dir(self.stat_out_dir)).grid(row=1, column=2, pady=15)

        tip_label = ttk.Label(self.tab_stat, text="说明：此功能将遍历该目录及所有子目录下的视频，并自动生成\n《视频详细数据.xlsx》和《视频总时长统计.xlsx》两份报表。", foreground="#555")
        tip_label.pack(pady=10)

        frame_status = ttk.Frame(self.tab_stat, padding=(20, 10))
        frame_status.pack(fill="x", pady=10)

        self.lbl_stat_status = ttk.Label(frame_status, textvariable=self.stat_status_text)
        self.lbl_stat_status.pack(anchor="w")

        self.stat_progress_bar = ttk.Progressbar(frame_status, orient="horizontal", mode="determinate", variable=self.stat_progress_var)
        self.stat_progress_bar.pack(fill="x", pady=5)

        frame_btn = ttk.Frame(self.tab_stat, padding=20)
        frame_btn.pack(fill="x")

        self.btn_run_stat = ttk.Button(frame_btn, text="开始统计", command=self.start_stat)
        self.btn_run_stat.pack(expand=True, ipadx=20, ipady=5)

    def start_stat(self):
        in_dir = self.stat_in_dir.get().strip()
        out_dir = self.stat_out_dir.get().strip()
        if not os.path.exists(in_dir) or not out_dir:
            messagebox.showerror("错误", "请检查需统计目录和表格输出目录是否正确！")
            return

        if not os.path.exists(self.ffprobe_bin):
            err_msg = (
                f"找不到 FFprobe 核心组件！\n\n"
                f"请确保在软件所在的目录下，有一个名为 'ffmpeg' 的文件夹，\n"
                f"并且里面包含了 'ffprobe.exe'。\n\n"
                f"预期路径：\n{self.ffprobe_bin}"
            )
            messagebox.showerror("环境缺失", err_msg)
            return

        os.makedirs(out_dir, exist_ok=True)
        self.is_stating = True
        self.btn_run_stat.config(state="disabled")
        self.stat_progress_var.set(0)

        threading.Thread(target=self.process_stat_thread, args=(in_dir, out_dir), daemon=True).start()

    def process_stat_thread(self, in_dir, out_dir):
        target_files = []
        for root_path, _, files in os.walk(in_dir):
            for f in files:
                if os.path.splitext(f)[1].lower() in self.supported_exts:
                    target_files.append(os.path.join(root_path, f))

        if not target_files:
            self.root.after(0, self.stat_reset_ui, "目标目录中未找到支持的视频文件。")
            return

        total_files = len(target_files)
        detailed_data = []
        total_seconds_precise = 0.0

        for i, filepath in enumerate(target_files):
            filename = os.path.basename(filepath)
            parent_dir = os.path.basename(os.path.dirname(filepath))

            duration = 0.0
            dur_min = 0.0
            resolution = ""
            bitrate = ""       
            v_bitrate = ""     
            fps = ""
            err_msg = ""

            try:
                cmd = [self.ffprobe_bin, '-v', 'quiet', '-print_format', 'json', '-show_format', '-show_streams', filepath]
                result = subprocess.run(
                    cmd, 
                    stdout=subprocess.PIPE, 
                    stderr=subprocess.PIPE, 
                    encoding='utf-8', 
                    errors='ignore',
                    creationflags=subprocess.CREATE_NO_WINDOW
                )
                
                output = result.stdout
                if not output or not output.strip():
                    raise ValueError(f"无法读取文件信息。可能是非视频文件或文件已损坏。")

                data = json.loads(output)

                if 'format' in data and 'duration' in data['format']:
                    duration = float(data['format']['duration'])
                    dur_min = duration / 60.0
                    total_seconds_precise += duration
                    
                    if 'bit_rate' in data['format']:
                        bitrate = f"{round(float(data['format']['bit_rate']) / 1000)} kbps"
                else:
                    raise ValueError("未检测到时长数据，可能是损坏文件。")

                video_stream = next((stream for stream in data.get('streams', []) if stream.get('codec_type') == 'video'), None)
                if video_stream:
                    if 'width' in video_stream and 'height' in video_stream:
                        resolution = f"{video_stream['width']}*{video_stream['height']}"
                    if 'r_frame_rate' in video_stream:
                        num, den = video_stream['r_frame_rate'].split('/')
                        if den != '0':
                            fps = str(round(float(num) / float(den), 2))
                    
                    if 'bit_rate' in video_stream:
                        v_bitrate = f"{round(float(video_stream['bit_rate']) / 1000)} kbps"

            except Exception as e:
                err_msg = str(e)

            detailed_data.append({
                "文件路径": filepath.replace('\\', '/'),
                "子目录": parent_dir,
                "文件名": filename,
                "时长（秒）": round(duration, 3) if not err_msg else "",
                "时长（分）": round(dur_min, 3) if not err_msg else "",
                "分辨率（宽*高）": resolution,
                "总码率": bitrate,
                "视频流码率": v_bitrate,
                "帧率": fps,
                "处理错误": err_msg
            })

            percent = ((i + 1) / total_files) * 100
            self.root.after(0, self.stat_status_text.set, f"正在分析 ({i+1}/{total_files}): {filename}")
            self.root.after(0, self.stat_progress_var.set, percent)

        try:
            self.root.after(0, self.stat_status_text.set, "正在生成 Excel 表格...")
            
            df1 = pd.DataFrame(detailed_data)
            out1 = os.path.join(out_dir, "视频详细数据.xlsx")
            df1.to_excel(out1, index=False)

            df2 = pd.DataFrame([{
                "总时长（秒）": total_seconds_precise,
                "总时长（分）": total_seconds_precise / 60.0,
                "总时长（小时）": total_seconds_precise / 3600.0
            }])
            out2 = os.path.join(out_dir, "视频总时长统计.xlsx")
            df2.to_excel(out2, index=False)

            self.root.after(0, self.stat_progress_var.set, 100)
            self.root.after(0, self.stat_reset_ui, "统计与表格生成完毕！", True)

        except Exception as e:
            self.root.after(0, self.stat_reset_ui, f"导出表格失败: {str(e)}")

    def stat_reset_ui(self, message, success=False):
        self.is_stating = False
        self.btn_run_stat.config(state="normal")
        self.stat_status_text.set(message)
        if success:
            messagebox.showinfo("完成", message)
        else:
            messagebox.showwarning("提示", message)

    # ================= 原工具逻辑与联动保留 =================
    def update_zip_ui(self):
        if self.output_as_zip.get():
            self.lbl_zip.state(['!disabled'])
            self.entry_zip_max.state(['!disabled'])
        else:
            self.lbl_zip.state(['disabled'])
            self.entry_zip_max.state(['disabled'])

    def on_prop_w_check(self):
        if self.prop_w_en.get(): self.prop_h_en.set(False)
        self.update_res_ui()

    def on_prop_h_check(self):
        if self.prop_h_en.get(): self.prop_w_en.set(False)
        self.update_res_ui()

    def update_res_ui(self, *args):
        mode = self.res_mode.get()
        if mode == 1:
            self.cb_prop_w.state(['disabled'])
            self.cb_prop_h.state(['disabled'])
            self.entry_prop_w.state(['disabled'])
            self.entry_prop_h.state(['disabled'])
            self.entry_exact_w.state(['disabled'])
            self.entry_exact_h.state(['disabled'])
        elif mode == 2:
            self.cb_prop_w.state(['!disabled'])
            self.cb_prop_h.state(['!disabled'])
            self.entry_exact_w.state(['disabled'])
            self.entry_exact_h.state(['disabled'])
            
            if self.prop_w_en.get():
                self.entry_prop_w.state(['!disabled'])
                self.entry_prop_h.state(['disabled'])
            elif self.prop_h_en.get():
                self.entry_prop_h.state(['!disabled'])
                self.entry_prop_w.state(['disabled'])
            else:
                self.entry_prop_w.state(['disabled'])
                self.entry_prop_h.state(['disabled'])
        elif mode == 3:
            self.cb_prop_w.state(['disabled'])
            self.cb_prop_h.state(['disabled'])
            self.entry_prop_w.state(['disabled'])
            self.entry_prop_h.state(['disabled'])
            self.entry_exact_w.state(['!disabled'])
            self.entry_exact_h.state(['!disabled'])

    def browse_dir(self, var):
        folder = filedialog.askdirectory()
        if folder: var.set(folder)

    def time_to_seconds(self, h, m, s):
        return int(h) * 3600 + int(m) * 60 + float(s)

    def stop_processing(self):
        if self.is_processing:
            self.is_cancelled = True
            self.status_text.set("正在中止处理，请稍候...")
            self.btn_stop.config(state="disabled")
            if self.current_process:
                try:
                    self.current_process.kill()
                except Exception:
                    pass
                    
    def get_video_stream_bitrate(self, filepath):
        """核心辅助：智能获取视频流原码率"""
        try:
            cmd = [self.ffprobe_bin, '-v', 'quiet', '-print_format', 'json', '-show_streams', '-select_streams', 'v:0', filepath]
            result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, encoding='utf-8', errors='ignore', creationflags=subprocess.CREATE_NO_WINDOW)
            data = json.loads(result.stdout)
            if 'streams' in data and len(data['streams']) > 0:
                stream = data['streams'][0]
                if 'bit_rate' in stream:
                    return f"{stream['bit_rate']}"
            
            # 如果流中没有 bit_rate，尝试从总 format 中获取，并扣除音频的预估值
            cmd_fmt = [self.ffprobe_bin, '-v', 'quiet', '-print_format', 'json', '-show_format', filepath]
            result_fmt = subprocess.run(cmd_fmt, stdout=subprocess.PIPE, stderr=subprocess.PIPE, encoding='utf-8', errors='ignore', creationflags=subprocess.CREATE_NO_WINDOW)
            data_fmt = json.loads(result_fmt.stdout)
            if 'format' in data_fmt and 'bit_rate' in data_fmt['format']:
                total_br = int(data_fmt['format']['bit_rate'])
                # 假设音频192k左右，扣除192k，至少保留100k作为兜底视频码率
                v_br = max(total_br - 192000, 100000)
                return str(v_br)
        except Exception:
            pass
        return None

    def get_audio_stream_bitrate(self, filepath):
        """核心辅助：智能获取音频流原码率"""
        try:
            cmd = [self.ffprobe_bin, '-v', 'quiet', '-print_format', 'json', '-show_streams', '-select_streams', 'a:0', filepath]
            result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, encoding='utf-8', errors='ignore', creationflags=subprocess.CREATE_NO_WINDOW)
            data = json.loads(result.stdout)
            if 'streams' in data and len(data['streams']) > 0:
                stream = data['streams'][0]
                if 'bit_rate' in stream:
                    return f"{stream['bit_rate']}"
        except Exception:
            pass
        return None

    def start_processing(self):
        in_dir, out_dir = self.in_dir.get(), self.out_dir.get()
        if not os.path.exists(in_dir) or not out_dir:
            messagebox.showerror("错误", "请检查输入输出路径是否正确！")
            return

        if not os.path.exists(self.ffmpeg_bin):
            err_msg = (
                f"找不到 FFmpeg 核心组件！\n\n"
                f"请确保在软件所在的目录下，有一个名为 'ffmpeg' 的文件夹，\n"
                f"并且里面包含了 'ffmpeg.exe'。\n\n"
                f"预期路径：\n{self.ffmpeg_bin}"
            )
            messagebox.showerror("环境缺失", err_msg)
            return
        
        if self.output_as_zip.get():
            try:
                max_f = int(self.zip_max_var.get().strip())
                if max_f <= 0: raise ValueError
            except:
                messagebox.showwarning("警告", "最大打包文件数必须是大于 0 的整数！")
                return

        os.makedirs(out_dir, exist_ok=True)
        self.is_processing = True
        self.is_cancelled = False
        
        self.btn_run.config(state="disabled")
        self.btn_stop.config(state="normal")
        self.progress_var.set(0)
        
        threading.Thread(target=self.process_videos_thread, args=(in_dir, out_dir), daemon=True).start()

    def process_videos_thread(self, in_dir, out_dir):
        files = [f for f in os.listdir(in_dir) if os.path.splitext(f)[1].lower() in self.supported_exts]
        if not files:
            self.root.after(0, self.reset_ui_state, "输入目录中未找到支持的视频文件。")
            return

        target_fmt = self.target_fmt_var.get()
        total_files = len(files)

        actual_out_dir = out_dir
        if self.output_as_zip.get():
            actual_out_dir = os.path.join(out_dir, "_temp_ffmpeg_zip_cache")
            os.makedirs(actual_out_dir, exist_ok=True)

        for i, filename in enumerate(files):
            if self.is_cancelled:
                break

            in_file = os.path.join(in_dir, filename)
            file_name_without_ext, original_ext = os.path.splitext(filename)
            
            # === 核心修改：针对每个文件动态决定真实编码器 ===
            vcodec = self.codec_var.get()
            hw_choice = self.encoder_var.get()
            
            if hw_choice == "纯转封装 (极速)":
                encoder = "copy"
            else:
                if vcodec == "保持原始":
                    vcodec = self.get_video_codec(in_file) # 动态探测！
                    
                if vcodec == "H.265":
                    if "NVIDIA" in hw_choice: encoder = "hevc_nvenc"
                    elif "AMD" in hw_choice: encoder = "hevc_amf"
                    elif "Intel" in hw_choice: encoder = "hevc_qsv"
                    elif "Apple" in hw_choice: encoder = "hevc_videotoolbox"
                    else: encoder = "libx265"
                else: # H.264
                    if "NVIDIA" in hw_choice: encoder = "h264_nvenc"
                    elif "AMD" in hw_choice: encoder = "h264_amf"
                    elif "Intel" in hw_choice: encoder = "h264_qsv"
                    elif "Apple" in hw_choice: encoder = "h264_videotoolbox"
                    else: encoder = "libx264"
            if self.is_cancelled:
                break

            in_file = os.path.join(in_dir, filename)
            file_name_without_ext, original_ext = os.path.splitext(filename)
            
            if target_fmt == "保持原格式":
                out_ext = original_ext
            else:
                out_ext = "." + target_fmt.lower()

            out_file = os.path.join(actual_out_dir, file_name_without_ext + out_ext)
            
            cmd = [self.ffmpeg_bin, "-y", "-i", in_file, "-c:v", encoder]
            
            if encoder != "copy":
                res_mode = self.res_mode.get()
                scale_filter = ""
                if res_mode == 2:
                    w = self.prop_w.get() if self.prop_w_en.get() else "-2"
                    h = self.prop_h.get() if self.prop_h_en.get() else "-2"
                    if w != "-2" or h != "-2": scale_filter = f"scale={w}:{h}:flags=lanczos"
                elif res_mode == 3:
                    scale_filter = f"scale={self.exact_w.get()}:{self.exact_h.get()}:flags=lanczos"
                
                if scale_filter: cmd.extend(["-vf", scale_filter])

                if self.quality_mode.get() == 1:
                    cmd.extend(["-b:v", f"{self.bitrate.get()}k"])
                elif self.quality_mode.get() == 2:
                    q_val = self.crf.get()
                    if "libx26" in encoder: cmd.extend(["-crf", q_val])
                    elif "nvenc" in encoder: cmd.extend(["-cq", q_val])
                    elif "amf" in encoder: cmd.extend(["-rc", "cqp", "-qp_i", q_val, "-qp_p", q_val, "-qp_b", q_val])
                    elif "qsv" in encoder: cmd.extend(["-global_quality", q_val])
                    elif "videotoolbox" in encoder: cmd.extend(["-q:v", q_val]) # 新增: Mac M芯片动态质量参数
                    else: cmd.extend(["-crf", q_val])
                elif self.quality_mode.get() == 3:
                    # 动态探测原视频流码率
                    orig_v_bitrate = self.get_video_stream_bitrate(in_file)
                    if orig_v_bitrate:
                        cmd.extend(["-b:v", orig_v_bitrate])
                    else:
                        cmd.extend(["-crf", "28"]) # 提取失败则回退默认
                elif self.quality_mode.get() == 4:
                    # === 智能限流核心逻辑 ===
                    try: target_br = int(self.max_bitrate.get())
                    except: target_br = 3000
                    orig_v_bitrate = self.get_video_stream_bitrate(in_file)
                    if orig_v_bitrate:
                        orig_br_kbps = int(orig_v_bitrate) // 1000
                        if orig_br_kbps > target_br:
                            cmd.extend(["-b:v", f"{target_br}k"]) # 大于目标，截断为上限
                        else:
                            cmd.extend(["-b:v", orig_v_bitrate])  # 小于目标，保持原状
                    else:
                        cmd.extend(["-b:v", f"{target_br}k"])
                
                preset_val = self.preset.get()
                if encoder == "h264_amf":
                    amf_preset_map = {"fast": "speed", "medium": "balanced", "slow": "quality"}
                    cmd.extend(["-quality", amf_preset_map.get(preset_val, "balanced")])
                else:
                    cmd.extend(["-preset", preset_val])
                
                fps_val = self.fps_var.get()
                if fps_val != "保持原始":
                    cmd.extend(["-r", fps_val])

                threads_val = self.threads_var.get()
                if threads_val != "自动":
                    cmd.extend(["-threads", threads_val])

            if self.copy_audio.get() or encoder == "copy":
                cmd.extend(["-c:a", "copy"])
            else:
                audio_br = self.audio_bitrate_var.get().strip()
                if audio_br == "保持原始":
                    orig_a_br = self.get_audio_stream_bitrate(in_file)
                    if orig_a_br:
                        cmd.extend(["-c:a", "aac", "-b:a", orig_a_br])
                    else:
                        cmd.extend(["-c:a", "aac", "-b:a", "192k"]) # 获取失败兜底
                else:
                    cmd.extend(["-c:a", "aac", "-b:a", f"{audio_br}k"])

            if self.faststart.get() and out_ext.lower() in ['.mp4', '.mov']:
                cmd.extend(["-movflags", "+faststart"])

            if self.force_sync.get():
                cmd.extend(["-vsync", "1", "-async", "1"])

            cmd.append(out_file)

            self.root.after(0, self.status_text.set, f"正在处理 ({i+1}/{total_files}): {filename}")
            self.root.after(0, self.progress_var.set, 0)

            total_duration_sec = 0
            
            try:
                self.current_process = subprocess.Popen(
                    cmd, 
                    stderr=subprocess.PIPE, 
                    stdout=subprocess.PIPE, 
                    encoding='utf-8',   
                    errors='ignore',     
                    creationflags=subprocess.CREATE_NO_WINDOW
                )

                for line in self.current_process.stderr:
                    if self.is_cancelled:
                        self.current_process.kill() 
                        break
                    
                    if total_duration_sec == 0:
                        dur_match = self.re_duration.search(line)
                        if dur_match:
                            total_duration_sec = self.time_to_seconds(*dur_match.groups())

                    time_match = self.re_time.search(line)
                    if time_match and total_duration_sec > 0:
                        current_sec = self.time_to_seconds(*time_match.groups())
                        percent = min((current_sec / total_duration_sec) * 100, 100)
                        self.root.after(0, self.progress_var.set, percent)
                
                self.current_process.wait()

            except Exception as e:
                print(f"处理文件 {filename} 时发生异常: {e}")
                if self.current_process:
                    try:
                        self.current_process.kill()
                    except:
                        pass

            if self.is_cancelled:
                if os.path.exists(out_file):
                    try:
                        os.remove(out_file)
                    except OSError:
                        pass
                break 

        if self.is_cancelled:
            self.root.after(0, self.reset_ui_state, "处理已中止！未完成的文件已清理。")
            if self.output_as_zip.get() and os.path.exists(actual_out_dir):
                shutil.rmtree(actual_out_dir, ignore_errors=True)
        else:
            if self.output_as_zip.get():
                self.root.after(0, self.status_text.set, "视频处理完毕，正在进行分卷打包...")
                try:
                    max_f = int(self.zip_max_var.get().strip())
                    processed_files = [f for f in os.listdir(actual_out_dir) if os.path.isfile(os.path.join(actual_out_dir, f))]
                    processed_files.sort()
                    num_zips = math.ceil(len(processed_files) / max_f)

                    for j in range(num_zips):
                        chunk = processed_files[j*max_f : (j+1)*max_f]
                        first_name = os.path.splitext(chunk[0])[0]
                        last_name = os.path.splitext(chunk[-1])[0]

                        if first_name == last_name:
                            zip_filename = f"{first_name}.zip"
                        else:
                            zip_filename = f"{first_name}_{last_name}.zip"

                        zip_path = os.path.join(out_dir, zip_filename)
                        
                        with zipfile.ZipFile(zip_path, 'w', compression=zipfile.ZIP_STORED) as zf:
                            for f in chunk:
                                file_path = os.path.join(actual_out_dir, f)
                                zf.write(file_path, f)

                    shutil.rmtree(actual_out_dir, ignore_errors=True)
                    
                except Exception as e:
                    self.root.after(0, self.reset_ui_state, f"打包发生异常: {str(e)}", False)
                    return

            self.root.after(0, self.progress_var.set, 100)
            self.root.after(0, self.reset_ui_state, "所有视频处理完毕！", True)

    def reset_ui_state(self, message, success=False):
        self.is_processing = False
        self.current_process = None
        self.btn_run.config(state="normal")
        self.btn_stop.config(state="disabled")
        self.status_text.set(message)
        if success:
            messagebox.showinfo("完成", message)
        else:
            messagebox.showwarning("提示", message)

    # ================= 音频提取 UI 与逻辑 =================
    def setup_audio_ui(self):
        self.audio_in_dir = tk.StringVar()
        self.audio_out_dir = tk.StringVar()
        self.audio_fmt_var = tk.StringVar(value="MP3 (有损 192kbps)")
        self.audio_progress_var = tk.DoubleVar(value=0)
        self.audio_status_text = tk.StringVar(value="等待开始...")
        self.is_audio_processing = False

        frame_dir = ttk.Frame(self.tab_audio, padding=20)
        frame_dir.pack(fill="x")
        frame_dir.columnconfigure(1, weight=1)

        ttk.Label(frame_dir, text="视频输入目录:").grid(row=0, column=0, sticky="e", pady=15)
        ttk.Entry(frame_dir, textvariable=self.audio_in_dir).grid(row=0, column=1, sticky="we", padx=5, pady=15)
        ttk.Button(frame_dir, text="浏览...", command=lambda: self.browse_dir(self.audio_in_dir)).grid(row=0, column=2, pady=15)

        ttk.Label(frame_dir, text="音频输出目录:").grid(row=1, column=0, sticky="e", pady=15)
        ttk.Entry(frame_dir, textvariable=self.audio_out_dir).grid(row=1, column=1, sticky="we", padx=5, pady=15)
        ttk.Button(frame_dir, text="浏览...", command=lambda: self.browse_dir(self.audio_out_dir)).grid(row=1, column=2, pady=15)

        frame_opts = ttk.LabelFrame(self.tab_audio, text="提取设置", padding=10)
        frame_opts.pack(fill="x", padx=20, pady=5)
        
        ttk.Label(frame_opts, text="音频输出格式:").pack(side="left", padx=5)
        cb_fmt = ttk.Combobox(frame_opts, textvariable=self.audio_fmt_var, values=["MP3 (有损 192kbps)", "WAV (无损 PCM)"], state="readonly", width=25)
        cb_fmt.pack(side="left", padx=5)

        tip_label = ttk.Label(self.tab_audio, text="说明：该功能将自动剥离原视频的画面，只保留纯音频流。\n底层已默认开启强制时间轴同步，以保障输出音频时长与原视频完全一致。", foreground="#555")
        tip_label.pack(pady=15)

        frame_status = ttk.Frame(self.tab_audio, padding=(20, 10))
        frame_status.pack(fill="x", pady=10)

        self.lbl_audio_status = ttk.Label(frame_status, textvariable=self.audio_status_text)
        self.lbl_audio_status.pack(anchor="w")

        self.audio_progress_bar = ttk.Progressbar(frame_status, orient="horizontal", mode="determinate", variable=self.audio_progress_var)
        self.audio_progress_bar.pack(fill="x", pady=5)

        frame_btn = ttk.Frame(self.tab_audio, padding=20)
        frame_btn.pack(fill="x")

        self.btn_run_audio = ttk.Button(frame_btn, text="开始提取", command=self.start_audio)
        self.btn_run_audio.pack(side="left", expand=True, padx=10, ipadx=10, ipady=5)

        self.btn_stop_audio = ttk.Button(frame_btn, text="停止提取", command=self.stop_audio, state="disabled")
        self.btn_stop_audio.pack(side="right", expand=True, padx=10, ipadx=10, ipady=5)

    def stop_audio(self):
        if self.is_audio_processing:
            self.is_cancelled = True
            self.audio_status_text.set("正在中止提取，请稍候...")
            self.btn_stop_audio.config(state="disabled")
            if self.current_process:
                try:
                    self.current_process.kill()
                except Exception:
                    pass

    def start_audio(self):
        in_dir = self.audio_in_dir.get().strip()
        out_dir = self.audio_out_dir.get().strip()
        if not os.path.exists(in_dir) or not out_dir:
            messagebox.showerror("错误", "请检查视频输入目录和音频输出目录是否正确！")
            return

        if not os.path.exists(self.ffmpeg_bin):
            messagebox.showerror("环境缺失", f"找不到 {self.ffmpeg_bin}！")
            return

        os.makedirs(out_dir, exist_ok=True)
        self.is_audio_processing = True
        self.is_cancelled = False
        
        self.btn_run_audio.config(state="disabled")
        self.btn_stop_audio.config(state="normal")
        self.audio_progress_var.set(0)
        
        threading.Thread(target=self.process_audio_thread, args=(in_dir, out_dir), daemon=True).start()

    def process_audio_thread(self, in_dir, out_dir):
        files = [f for f in os.listdir(in_dir) if os.path.splitext(f)[1].lower() in self.supported_exts]
        if not files:
            self.root.after(0, self.audio_reset_ui, "目标目录中未找到支持的视频文件。")
            return

        fmt_choice = self.audio_fmt_var.get()
        is_wav = "WAV" in fmt_choice
        ext = ".wav" if is_wav else ".mp3"
        total_files = len(files)

        for i, filename in enumerate(files):
            if self.is_cancelled:
                break

            in_file = os.path.join(in_dir, filename)
            file_name_without_ext = os.path.splitext(filename)[0]
            out_file = os.path.join(out_dir, file_name_without_ext + ext)

            cmd = [self.ffmpeg_bin, "-y", "-i", in_file, "-vn"]
            
            if is_wav:
                cmd.extend(["-c:a", "pcm_s16le"])
            else:
                cmd.extend(["-c:a", "libmp3lame", "-b:a", "192k"])

            cmd.extend(["-async", "1"])
            cmd.append(out_file)

            self.root.after(0, self.audio_status_text.set, f"正在提取 ({i+1}/{total_files}): {filename}")
            self.root.after(0, self.audio_progress_var.set, 0)

            total_duration_sec = 0

            try:
                self.current_process = subprocess.Popen(
                    cmd, 
                    stderr=subprocess.PIPE, 
                    stdout=subprocess.PIPE, 
                    encoding='utf-8', 
                    errors='ignore',
                    creationflags=subprocess.CREATE_NO_WINDOW
                )

                for line in self.current_process.stderr:
                    if self.is_cancelled:
                        self.current_process.kill()
                        break
                    
                    if total_duration_sec == 0:
                        dur_match = self.re_duration.search(line)
                        if dur_match:
                            total_duration_sec = self.time_to_seconds(*dur_match.groups())

                    time_match = self.re_time.search(line)
                    if time_match and total_duration_sec > 0:
                        current_sec = self.time_to_seconds(*time_match.groups())
                        percent = min((current_sec / total_duration_sec) * 100, 100)
                        self.root.after(0, self.audio_progress_var.set, percent)

                self.current_process.wait()

            except Exception as e:
                print(f"提取文件 {filename} 时发生异常: {e}")
                if self.current_process:
                    try:
                        self.current_process.kill()
                    except:
                        pass

            if self.is_cancelled:
                if os.path.exists(out_file):
                    try:
                        os.remove(out_file)
                    except OSError:
                        pass
                break

        if self.is_cancelled:
            self.root.after(0, self.audio_reset_ui, "提取已中止！未完成的文件已清理。")
        else:
            self.root.after(0, self.audio_progress_var.set, 100)
            self.root.after(0, self.audio_reset_ui, "所有音频提取完毕！", True)

    def audio_reset_ui(self, message, success=False):
        self.is_audio_processing = False
        self.current_process = None
        self.btn_run_audio.config(state="normal")
        self.btn_stop_audio.config(state="disabled")
        self.audio_status_text.set(message)
        if success:
            messagebox.showinfo("完成", message)
        else:
            messagebox.showwarning("提示", message)

    # ================= 新增：音视频合并 UI 与逻辑 =================
    def setup_merge_ui(self):
        self.m_video_in = tk.StringVar()
        self.m_voice_in = tk.StringVar()
        self.m_bgm_in = tk.StringVar()
        self.m_sub_in = tk.StringVar()
        self.m_out_dir = tk.StringVar()
        
        # 音频互斥模式：1为手动音量，2为自动平衡，3为原始拷贝
        self.m_audio_mode = tk.IntVar(value=1)
        
        self.m_orig_vol = tk.StringVar(value="100%")
        self.m_voice_vol = tk.StringVar(value="100%")
        self.m_bgm_vol = tk.StringVar(value="30%")
        
        self.m_keep_orig_audio = tk.BooleanVar(value=False)
        
        self.m_font_name = tk.StringVar()
        self.m_font_size = tk.StringVar(value="24")
        self.m_font_outline = tk.StringVar(value="1")
        self.m_font_marginv = tk.StringVar(value="10")

        self.m_progress_var = tk.DoubleVar(value=0)
        self.m_status_text = tk.StringVar(value="等待开始...")
        self.is_merge_processing = False

        # --- 目录选择区 ---
        frame_dir = ttk.Frame(self.tab_merge, padding=(10, 5))
        frame_dir.pack(fill="x")
        frame_dir.columnconfigure(1, weight=1)

        ttk.Label(frame_dir, text="视频目录:").grid(row=0, column=0, sticky="e", pady=5)
        ttk.Entry(frame_dir, textvariable=self.m_video_in).grid(row=0, column=1, sticky="we", padx=5)
        ttk.Button(frame_dir, text="浏览...", command=lambda: self.browse_dir(self.m_video_in)).grid(row=0, column=2)

        ttk.Label(frame_dir, text="干声目录 (可选):").grid(row=1, column=0, sticky="e", pady=5)
        ttk.Entry(frame_dir, textvariable=self.m_voice_in).grid(row=1, column=1, sticky="we", padx=5)
        ttk.Button(frame_dir, text="浏览...", command=lambda: self.browse_dir(self.m_voice_in)).grid(row=1, column=2)

        ttk.Label(frame_dir, text="BGM目录 (可选):").grid(row=2, column=0, sticky="e", pady=5)
        ttk.Entry(frame_dir, textvariable=self.m_bgm_in).grid(row=2, column=1, sticky="we", padx=5)
        ttk.Button(frame_dir, text="浏览...", command=lambda: self.browse_dir(self.m_bgm_in)).grid(row=2, column=2)

        ttk.Label(frame_dir, text="字幕目录 (可选):").grid(row=3, column=0, sticky="e", pady=5)
        ttk.Entry(frame_dir, textvariable=self.m_sub_in).grid(row=3, column=1, sticky="we", padx=5)
        ttk.Button(frame_dir, text="浏览...", command=lambda: self.browse_dir(self.m_sub_in)).grid(row=3, column=2)
        
        ttk.Label(frame_dir, text="合并输出目录:").grid(row=4, column=0, sticky="e", pady=5)
        ttk.Entry(frame_dir, textvariable=self.m_out_dir).grid(row=4, column=1, sticky="we", padx=5)
        ttk.Button(frame_dir, text="浏览...", command=lambda: self.browse_dir(self.m_out_dir)).grid(row=4, column=2)

        # --- 设置参数区 ---
        frame_settings = ttk.Frame(self.tab_merge, padding=(10, 0))
        frame_settings.pack(fill="both", expand=True)

        # 左侧：音频与字幕样式
        lf_left = ttk.LabelFrame(frame_settings, text="音轨互斥策略与字幕(SRT)设定", padding=10)
        lf_left.pack(side="left", fill="both", expand=True, padx=(0, 5))
        
        ttk.Radiobutton(lf_left, text="手动设置音量(已将对数映射至百分比):", variable=self.m_audio_mode, value=1, command=self.update_m_audio_ui).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0,2))
        
        frame_vols = ttk.Frame(lf_left)
        frame_vols.grid(row=1, column=0, columnspan=2, sticky="w", padx=(20, 0), pady=2)
        
        ttk.Label(frame_vols, text="原声:").pack(side="left")
        self.m_cb_ovol = ttk.Combobox(frame_vols, textvariable=self.m_orig_vol, values=["200%", "150%", "125%","100%", "80%", "70%","65%", "0% (静音)"], width=7)
        self.m_cb_ovol.pack(side="left", padx=(0, 8))
        
        ttk.Label(frame_vols, text="干声:").pack(side="left")
        self.m_cb_vvol = ttk.Combobox(frame_vols, textvariable=self.m_voice_vol, values=["200%", "150%", "125%","100%", "80%", "70%","65%", "0% (静音)"], width=7)
        self.m_cb_vvol.pack(side="left", padx=(0, 8))
        
        ttk.Label(frame_vols, text="BGM:").pack(side="left")
        self.m_cb_bvol = ttk.Combobox(frame_vols, textvariable=self.m_bgm_vol, values=["200%", "150%", "125%","100%", "80%", "70%","65%", "0% (静音)"], width=7)
        self.m_cb_bvol.pack(side="left")

        chk_keep = ttk.Checkbutton(lf_left, text="合并外部音频时，仍保留原视频音轨", variable=self.m_keep_orig_audio, command=self.update_m_audio_ui)
        chk_keep.grid(row=2, column=0, columnspan=2, sticky="w", pady=(0, 2))

        ttk.Radiobutton(lf_left, text="混合后整体平衡 (尽量保持原响度，不可手动调整)", variable=self.m_audio_mode, value=2, command=self.update_m_audio_ui).grid(row=3, column=0, columnspan=2, sticky="w", pady=2)
        
        # === 新增：方案一 (独立标准化+按比例混合) 及 绝对标准自定义 ===
        f_mode4 = ttk.Frame(lf_left)
        f_mode4.grid(row=4, column=0, columnspan=2, sticky="w", pady=2)
        ttk.Radiobutton(f_mode4, text="独立标准化各音轨后再混合", variable=self.m_audio_mode, value=4, command=self.update_m_audio_ui).pack(side="left")
        
        ttk.Label(f_mode4, text=" 目标标准(LUFS):").pack(side="left", padx=(10, 2))
        self.m_target_lufs = tk.StringVar(value="-24.0") # EBU R128 国际广播标准默认为 -24
        self.m_spin_lufs = ttk.Spinbox(f_mode4, from_=-70.0, to=-5.0, increment=1.0, textvariable=self.m_target_lufs, width=6)
        self.m_spin_lufs.pack(side="left")

        ttk.Radiobutton(lf_left, text="保持原始音频流(单轨使用)", variable=self.m_audio_mode, value=3, command=self.update_m_audio_ui).grid(row=5, column=0, columnspan=2, sticky="w", pady=(2, 5))
        
        # === 音频轨道偏移与拉伸适应 UI ===
        f_time = ttk.Frame(lf_left)
        f_time.grid(row=6, column=0, columnspan=2, sticky="w", pady=2)
        ttk.Label(f_time, text="音频偏移(ms):").pack(side="left")
        self.m_entry_offset = ttk.Entry(f_time, textvariable=self.m_audio_offset, width=5)
        self.m_entry_offset.pack(side="left", padx=(2, 8))
        self.m_cb_tempo = ttk.Checkbutton(f_time, text="拉伸自适应", variable=self.m_force_tempo, command=self.on_m_tempo_check)
        self.m_cb_tempo.pack(side="left", padx=(0, 8))
        self.m_cb_crop = ttk.Checkbutton(f_time, text="直接裁剪尾巴", variable=self.m_force_crop, command=self.on_m_crop_check)
        self.m_cb_crop.pack(side="left")

        ttk.Separator(lf_left, orient='horizontal').grid(row=7, column=0, columnspan=2, sticky="we", pady=5)
        ttk.Label(lf_left, text="(ASS格式自动保留原生样式)", foreground="#888").grid(row=8, column=0, columnspan=2, sticky="w")
        
        ttk.Label(lf_left, text="SRT字体:").grid(row=9, column=0, sticky="w", pady=2)
        
        frame_font = ttk.Frame(lf_left)
        frame_font.grid(row=9, column=1, sticky="w")
        font_families = list(tkfont.families())
        font_families.sort()
        default_font = "SimHei" if "SimHei" in font_families else (font_families[0] if font_families else "Arial")
        self.m_font_name.set(default_font)
        self.m_cb_font = ttk.Combobox(frame_font, textvariable=self.m_font_name, values=font_families, width=35)
        self.m_cb_font.pack(side="left")
        ttk.Button(frame_font, text="浏览...", width=6, command=self.browse_font).pack(side="left", padx=(5,0))
        
        ttk.Label(lf_left, text="SRT大小:").grid(row=10, column=0, sticky="w", pady=2)
        ttk.Spinbox(lf_left, from_=10, to=100, textvariable=self.m_font_size, width=10).grid(row=10, column=1, sticky="w")
        
        ttk.Label(lf_left, text="SRT描边:").grid(row=11, column=0, sticky="w", pady=2)
        ttk.Spinbox(lf_left, from_=0, to=10, textvariable=self.m_font_outline, width=10).grid(row=11, column=1, sticky="w")
        
        ttk.Label(lf_left, text="SRT边距:").grid(row=12, column=0, sticky="w", pady=2)
        ttk.Spinbox(lf_left, from_=0, to=100, textvariable=self.m_font_marginv, width=10).grid(row=12, column=1, sticky="w")

        # 初始化时触发一次以确认界面排他状态
        self.update_m_audio_ui()

        # 右侧：视频输出设定
        lf_right = ttk.LabelFrame(frame_settings, text="视频输出设定", padding=10)
        lf_right.pack(side="right", fill="both", expand=True, padx=(5, 0))

        # 封装格式与视频编码 (同行)
        f_fmt_enc_m = ttk.Frame(lf_right)
        f_fmt_enc_m.grid(row=0, column=0, columnspan=2, sticky="we", pady=2)
        ttk.Label(f_fmt_enc_m, text="格式:").pack(side="left")
        cb_fmt_m = ttk.Combobox(f_fmt_enc_m, textvariable=self.m_fmt_var, values=self.m_fmt_options, state="readonly", width=6)
        cb_fmt_m.pack(side="left", padx=(0, 10))
        
        ttk.Label(f_fmt_enc_m, text="编码:").pack(side="left")
        cb_vcodec_m = ttk.Combobox(f_fmt_enc_m, textvariable=self.m_codec_var, values=["保持原始", "H.264", "H.265"], state="readonly", width=8)
        cb_vcodec_m.pack(side="left")

        # 硬件加速引擎
        f_hw_m = ttk.Frame(lf_right)
        f_hw_m.grid(row=1, column=0, columnspan=2, sticky="we", pady=2)
        ttk.Label(f_hw_m, text="硬件加速:").pack(side="left")
        cb_enc_m = ttk.Combobox(f_hw_m, textvariable=self.m_encoder_var, values=list(self.encoder_map.keys()), state="readonly", width=15)
        cb_enc_m.pack(side="left")

        ttk.Separator(lf_right, orient='horizontal').grid(row=2, column=0, columnspan=2, sticky="we", pady=5)

        # 合并界面的独立分辨率设置 (横向紧凑布局)
        frame_res_m = ttk.Frame(lf_right)
        frame_res_m.grid(row=3, column=0, columnspan=2, sticky="we", pady=2)

        ttk.Radiobutton(frame_res_m, text="保持原分辨率", variable=self.m_res_mode, value=1, command=self.update_m_res_ui).grid(row=0, column=0, columnspan=2, sticky="w")
        
        ttk.Radiobutton(frame_res_m, text="缩放:", variable=self.m_res_mode, value=2, command=self.update_m_res_ui).grid(row=1, column=0, sticky="w", pady=2)
        f_scale = ttk.Frame(frame_res_m)
        f_scale.grid(row=1, column=1, sticky="w")
        self.m_cb_prop_w = ttk.Checkbutton(f_scale, text="宽:", variable=self.m_prop_w_en, command=self.on_m_prop_w_check)
        self.m_cb_prop_w.pack(side="left")
        self.m_entry_prop_w = ttk.Entry(f_scale, textvariable=self.m_prop_w, width=6)
        self.m_entry_prop_w.pack(side="left", padx=(0, 5))
        self.m_cb_prop_h = ttk.Checkbutton(f_scale, text="高:", variable=self.m_prop_h_en, command=self.on_m_prop_h_check)
        self.m_cb_prop_h.pack(side="left")
        self.m_entry_prop_h = ttk.Entry(f_scale, textvariable=self.m_prop_h, width=6)
        self.m_entry_prop_h.pack(side="left")

        ttk.Radiobutton(frame_res_m, text="指定:", variable=self.m_res_mode, value=3, command=self.update_m_res_ui).grid(row=2, column=0, sticky="w", pady=2)
        f_exact = ttk.Frame(frame_res_m)
        f_exact.grid(row=2, column=1, sticky="w")
        ttk.Label(f_exact, text="宽:").pack(side="left")
        self.m_entry_exact_w = ttk.Entry(f_exact, textvariable=self.m_exact_w, width=6)
        self.m_entry_exact_w.pack(side="left", padx=(0, 5))
        ttk.Label(f_exact, text="高:").pack(side="left")
        self.m_entry_exact_h = ttk.Entry(f_exact, textvariable=self.m_exact_h, width=6)
        self.m_entry_exact_h.pack(side="left")
        
        ttk.Separator(lf_right, orient='horizontal').grid(row=4, column=0, columnspan=2, sticky="we", pady=5)

        # 码率设置 (同行横向布局)
        frame_q_m = ttk.Frame(lf_right)
        frame_q_m.grid(row=5, column=0, columnspan=2, sticky="we", pady=2)

        ttk.Radiobutton(frame_q_m, text="固定:", variable=self.m_quality_mode, value=1).grid(row=0, column=0, sticky="w")
        ttk.Entry(frame_q_m, textvariable=self.m_bitrate, width=5).grid(row=0, column=1, sticky="w", padx=(0, 2))

        ttk.Radiobutton(frame_q_m, text="上限:", variable=self.m_quality_mode, value=4).grid(row=0, column=2, sticky="w")
        ttk.Entry(frame_q_m, textvariable=self.m_max_bitrate, width=5).grid(row=0, column=3, sticky="w", padx=(0, 2))

        ttk.Radiobutton(frame_q_m, text="CRF:", variable=self.m_quality_mode, value=2).grid(row=0, column=4, sticky="w")
        ttk.Entry(frame_q_m, textvariable=self.m_crf, width=3).grid(row=0, column=5, sticky="w", padx=(0, 2))

        ttk.Radiobutton(frame_q_m, text="保原码率", variable=self.m_quality_mode, value=3).grid(row=0, column=6, sticky="w")
        # 视频帧率、预设、线程、音码 (极限单行横向排布)
        f_other_m = ttk.Frame(lf_right)
        f_other_m.grid(row=6, column=0, columnspan=2, sticky="we", pady=(5, 0))
        
        ttk.Label(f_other_m, text="FPS:").pack(side="left", pady=2)
        cb_fps_m = ttk.Combobox(f_other_m, textvariable=self.m_fps_var, values=self.fps_options, state="readonly", width=5)
        cb_fps_m.pack(side="left", padx=(0, 8))

        ttk.Label(f_other_m, text="预设:").pack(side="left", pady=2)
        cb_preset_m = ttk.Combobox(f_other_m, textvariable=self.m_preset, values=["fast", "medium", "slow"], state="readonly", width=6)
        cb_preset_m.pack(side="left", padx=(0, 8))

        ttk.Label(f_other_m, text="线程:").pack(side="left", pady=2)
        cb_threads_m = ttk.Combobox(f_other_m, textvariable=self.m_threads_var, values=self.threads_options, state="readonly", width=4)
        cb_threads_m.pack(side="left", padx=(0, 8))
        
        ttk.Label(f_other_m, text="音码:").pack(side="left", pady=2)
        cb_audio_br_m = ttk.Combobox(f_other_m, textvariable=self.m_audio_bitrate_var, values=["128", "192", "256", "320"], width=4)
        cb_audio_br_m.pack(side="left")

        self.update_m_res_ui() # 刷新合并选项卡分辨率UI

        # --- 合并标签页独有的打包设置区 ---
        # === 替换为新的底部紧凑操作区 (同行布局) ===
        frame_bottom_m = ttk.Frame(self.tab_merge, padding=(10, 10))
        frame_bottom_m.pack(fill="x", side="bottom", pady=5)
        frame_bottom_m.columnconfigure(4, weight=1) # 让进度条自动填充剩余横向空间

        ttk.Checkbutton(frame_bottom_m, text="打包ZIP分卷", variable=self.m_output_as_zip, command=self.update_m_zip_ui).grid(row=0, column=0, sticky="w")
        self.lbl_zip_m = ttk.Label(frame_bottom_m, text="上限:")
        self.lbl_zip_m.grid(row=0, column=1, sticky="w", padx=(5, 0))
        self.entry_zip_max_m = ttk.Entry(frame_bottom_m, textvariable=self.m_zip_max_var, width=5)
        self.entry_zip_max_m.grid(row=0, column=2, sticky="w", padx=(0, 10))
        self.update_m_zip_ui() # 初始化组件状态

        self.lbl_merge_status = ttk.Label(frame_bottom_m, textvariable=self.m_status_text)
        self.lbl_merge_status.grid(row=0, column=3, sticky="e", padx=(0, 5))

        self.m_progress_bar = ttk.Progressbar(frame_bottom_m, orient="horizontal", mode="determinate", variable=self.m_progress_var)
        self.m_progress_bar.grid(row=0, column=4, sticky="we", padx=10)

        self.btn_run_merge = ttk.Button(frame_bottom_m, text="开始合并", width=12, command=self.start_merge)
        self.btn_run_merge.grid(row=0, column=5, padx=5)

        self.btn_stop_merge = ttk.Button(frame_bottom_m, text="停止合并", width=12, command=self.stop_merge, state="disabled")
        self.btn_stop_merge.grid(row=0, column=6)
        # === 替换结束 ===

    def update_m_zip_ui(self):
        if self.m_output_as_zip.get():
            self.lbl_zip_m.state(['!disabled'])
            self.entry_zip_max_m.state(['!disabled'])
        else:
            self.lbl_zip_m.state(['disabled'])
            self.entry_zip_max_m.state(['disabled'])

    def on_m_prop_w_check(self):
        if self.m_prop_w_en.get(): self.m_prop_h_en.set(False)
        self.update_m_res_ui()

    def on_m_prop_h_check(self):
        if self.m_prop_h_en.get(): self.m_prop_w_en.set(False)
        self.update_m_res_ui()

    def update_m_res_ui(self, *args):
        mode = self.m_res_mode.get()
        if mode == 1:
            self.m_cb_prop_w.state(['disabled'])
            self.m_cb_prop_h.state(['disabled'])
            self.m_entry_prop_w.state(['disabled'])
            self.m_entry_prop_h.state(['disabled'])
            self.m_entry_exact_w.state(['disabled'])
            self.m_entry_exact_h.state(['disabled'])
        elif mode == 2:
            self.m_cb_prop_w.state(['!disabled'])
            self.m_cb_prop_h.state(['!disabled'])
            self.m_entry_exact_w.state(['disabled'])
            self.m_entry_exact_h.state(['disabled'])
            
            if self.m_prop_w_en.get():
                self.m_entry_prop_w.state(['!disabled'])
                self.m_entry_prop_h.state(['disabled'])
            elif self.m_prop_h_en.get():
                self.m_entry_prop_h.state(['!disabled'])
                self.m_entry_prop_w.state(['disabled'])
            else:
                self.m_entry_prop_w.state(['disabled'])
                self.m_entry_prop_h.state(['disabled'])
        elif mode == 3:
            self.m_cb_prop_w.state(['disabled'])
            self.m_cb_prop_h.state(['disabled'])
            self.m_entry_prop_w.state(['disabled'])
            self.m_entry_prop_h.state(['disabled'])
            self.m_entry_exact_w.state(['!disabled'])
            self.m_entry_exact_h.state(['!disabled'])

    def on_m_tempo_check(self):
        if self.m_force_tempo.get():
            self.m_force_crop.set(False)
        self.update_m_audio_ui()

    def on_m_crop_check(self):
        if self.m_force_crop.get():
            self.m_force_tempo.set(False)
        self.update_m_audio_ui()

    def update_m_audio_ui(self, *args):
        """互斥锁：控制手动音量下拉框的灰化禁用状态，及偏移/拉伸的互斥"""
        mode = self.m_audio_mode.get()
        
        # 核心修改：模式 1 和 模式 4 都需要点亮音量控制框！
        if mode == 1 or mode == 4:
            # 原声音轨必须在勾选“保留”后才允许调节音量
            if self.m_keep_orig_audio.get():
                self.m_cb_ovol.config(state="normal")
            else:
                self.m_cb_ovol.config(state="disabled")
                
            self.m_cb_vvol.config(state="normal")
            self.m_cb_bvol.config(state="normal")
        else:
            self.m_cb_ovol.config(state="disabled")
            self.m_cb_vvol.config(state="disabled")
            self.m_cb_bvol.config(state="disabled")

        # 核心修改：控制目标响度LUFS输入框的互斥
        if hasattr(self, 'm_spin_lufs'):
            if mode == 4:
                self.m_spin_lufs.config(state="normal")
            else:
                self.m_spin_lufs.config(state="disabled")

        # 处理偏移、拉伸和裁剪选项与“保持原始(模式3)”的互斥
        if mode == 3:
            self.m_entry_offset.config(state="disabled")
            self.m_cb_tempo.config(state="disabled")
            self.m_cb_crop.config(state="disabled")
        else:
            self.m_entry_offset.config(state="normal")
            self.m_cb_tempo.config(state="normal")
            self.m_cb_crop.config(state="normal")

    def browse_font(self):
        """选择外部自定义字体文件"""
        font_path = filedialog.askopenfilename(filetypes=[("字体文件", "*.ttf *.otf *.ttc")])
        if font_path:
            self.m_font_name.set(font_path)

    def stop_merge(self):
        if self.is_merge_processing:
            self.is_cancelled = True
            self.m_status_text.set("正在中止合并，请稍候...")
            self.btn_stop_merge.config(state="disabled")
            if self.current_process:
                try:
                    self.current_process.kill()
                except Exception:
                    pass

    def start_merge(self):
        v_dir = self.m_video_in.get().strip()
        out_dir = self.m_out_dir.get().strip()
        if not os.path.exists(v_dir) or not out_dir:
            messagebox.showerror("错误", "请检查视频输入目录和输出目录是否正确！")
            return

        if not os.path.exists(self.ffmpeg_bin):
            messagebox.showerror("环境缺失", f"找不到 {self.ffmpeg_bin}！")
            return

        if self.m_output_as_zip.get():
            try:
                max_f = int(self.m_zip_max_var.get().strip())
                if max_f <= 0: raise ValueError
            except:
                messagebox.showwarning("警告", "最大打包文件数必须是大于 0 的整数！")
                return

        os.makedirs(out_dir, exist_ok=True)
        self.is_merge_processing = True
        self.is_cancelled = False
        
        self.btn_run_merge.config(state="disabled")
        self.btn_stop_merge.config(state="normal")
        self.m_progress_var.set(0)
        
        threading.Thread(target=self.process_merge_thread, daemon=True).start()

    def process_merge_thread(self):
        v_dir = self.m_video_in.get().strip()
        a_dir1 = self.m_voice_in.get().strip()
        a_dir2 = self.m_bgm_in.get().strip()
        s_dir = self.m_sub_in.get().strip()
        out_dir = self.m_out_dir.get().strip()

        video_files = [f for f in os.listdir(v_dir) if os.path.splitext(f)[1].lower() in self.supported_exts]
        if not video_files:
            self.root.after(0, self.merge_reset_ui, "视频目录中未找到支持的视频文件。")
            return

        total_files = len(video_files)
        
        # === 新增：全局 ASS 字体秒扫预检机制 ===
        if s_dir and os.path.exists(s_dir):
            self.root.after(0, self.m_status_text.set, "正在全局预检 ASS 字体完整性...")
            global_missing_fonts = set()
            sys_fonts = [f.lower() for f in tkfont.families()]
            
            for v_filename in video_files:
                base_name = os.path.splitext(v_filename)[0]
                s_path_check = os.path.join(s_dir, base_name + '.ass')
                if os.path.exists(s_path_check):
                    try:
                        with open(s_path_check, 'r', encoding='utf-8', errors='ignore') as f:
                            content = f.read()
                            # 提取样式表字体
                            for match in re.finditer(r"^Style:\s*[^,]+,\s*([^,]+)", content, re.MULTILINE):
                                font = match.group(1).strip()
                                check_font = font.lstrip('@').replace('"', '').replace("'", "")
                                if check_font and check_font.lower() not in sys_fonts:
                                    global_missing_fonts.add(font)
                            # 提取内联特效字体
                            for match in re.finditer(r"\\fn([^\\}]+)", content):
                                font = match.group(1).strip()
                                check_font = font.lstrip('@').replace('"', '').replace("'", "")
                                if check_font and check_font.lower() not in sys_fonts:
                                    global_missing_fonts.add(font)
                    except Exception as e:
                        print(f"预检 ASS 警告: {e}")
            
            if global_missing_fonts:
                err_msg = "在正式开始处理前，检测到以下 ASS 字幕使用了系统未安装的字体：\n\n"
                err_msg += "\n".join(list(global_missing_fonts))
                err_msg += "\n\n为防止字幕特效排版错乱，已安全中止任务！\n请在电脑上安装上述字体后，再次点击开始。"
                
                self.root.after(0, messagebox.showerror, "ASS字体缺失 (全局预检拦截)", err_msg)
                self.root.after(0, self.merge_reset_ui, "因字体缺失，合并任务已取消。")
                return # 预检不通过，直接退出，绝对不生成垃圾文件
        # === 全局预检结束 ===

        processed_count = 0

        actual_out_dir = out_dir
        if self.m_output_as_zip.get():
            actual_out_dir = os.path.join(out_dir, "_temp_ffmpeg_zip_cache_merge")
            os.makedirs(actual_out_dir, exist_ok=True)

        for i, v_filename in enumerate(video_files):
            if self.is_cancelled:
                break

            base_name = os.path.splitext(v_filename)[0]
            v_path = os.path.join(v_dir, v_filename)
            
            # === 核心修改：合并时针对主视频探测真实编码器 ===
            vcodec = self.m_codec_var.get()
            hw_choice = self.m_encoder_var.get()
            
            if hw_choice == "纯转封装 (极速)":
                encoder = "copy"
            else:
                if vcodec == "保持原始":
                    vcodec = self.get_video_codec(v_path) # 动态探测！
                    
                if vcodec == "H.265":
                    if "NVIDIA" in hw_choice: encoder = "hevc_nvenc"
                    elif "AMD" in hw_choice: encoder = "hevc_amf"
                    elif "Intel" in hw_choice: encoder = "hevc_qsv"
                    elif "Apple" in hw_choice: encoder = "hevc_videotoolbox"
                    else: encoder = "libx265"
                else:
                    if "NVIDIA" in hw_choice: encoder = "h264_nvenc"
                    elif "AMD" in hw_choice: encoder = "h264_amf"
                    elif "Intel" in hw_choice: encoder = "h264_qsv"
                    elif "Apple" in hw_choice: encoder = "h264_videotoolbox"
                    else: encoder = "libx264"
            
            # 智能匹配干声
            a1_path = None
            if a_dir1 and os.path.exists(a_dir1):
                for ext in ['.wav', '.mp3', '.flac', '.aac', '.m4a']:
                    temp = os.path.join(a_dir1, base_name + ext)
                    if os.path.exists(temp):
                        a1_path = temp
                        break
                # === 补回：如果找不到同名文件，但目录下只有1个音频，则作为全局通用音频 ===
                if not a1_path:
                    a1_files = [f for f in os.listdir(a_dir1) if os.path.splitext(f)[1].lower() in ['.wav', '.mp3', '.flac', '.aac', '.m4a']]
                    if len(a1_files) == 1:
                        a1_path = os.path.join(a_dir1, a1_files[0])
                        
            # 智能匹配BGM
            a2_path = None
            if a_dir2 and os.path.exists(a_dir2):
                for ext in ['.wav', '.mp3', '.flac', '.aac', '.m4a']:
                    temp = os.path.join(a_dir2, base_name + ext)
                    if os.path.exists(temp):
                        a2_path = temp
                        break
                # === 补回：如果找不到同名文件，但目录下只有1个音频，则作为全局通用音频 ===
                if not a2_path:
                    a2_files = [f for f in os.listdir(a_dir2) if os.path.splitext(f)[1].lower() in ['.wav', '.mp3', '.flac', '.aac', '.m4a']]
                    if len(a2_files) == 1:
                        a2_path = os.path.join(a_dir2, a2_files[0])

            # 智能匹配字幕
            s_path = None
            is_ass = False
            if s_dir and os.path.exists(s_dir):
                for ext in ['.ass', '.srt']:
                    temp = os.path.join(s_dir, base_name + ext)
                    if os.path.exists(temp):
                        s_path = temp
                        is_ass = (ext == '.ass')
                        break
            
            # 至少有音频或字幕再处理，否则原封不动输出意义不大
            if not a1_path and not a2_path and not s_path:
                continue

            processed_count += 1
            
            # 保持原格式处理
            if self.m_fmt_var.get() == "保持原格式":
                out_ext = os.path.splitext(v_filename)[1]
            else:
                out_ext = "." + self.m_fmt_var.get().lower()
                
            out_file = os.path.join(actual_out_dir, base_name + out_ext)

            # 构建合并指令组合
            cmd = [self.ffmpeg_bin, "-y", "-i", v_path]
            
            input_idx = 1
            v_idx = -1
            b_idx = -1
            
            if a1_path:
                cmd.extend(["-i", a1_path])
                v_idx = input_idx
                input_idx += 1
            if a2_path:
                cmd.extend(["-i", a2_path])
                b_idx = input_idx
                input_idx += 1

            fc_parts = []
            v_out = "0:v:0?"
            # === 核心修复：严格遵照勾选框决定原声去留 ===
            if self.m_keep_orig_audio.get():
                a_out = "0:a:0?"
            else:
                a_out = ""  # 只要没勾选，默认剥离/静音原声

            # -- 智能分辨率缩放 --
            scale_filter = ""
            if self.m_res_mode.get() == 2:
                w = self.m_prop_w.get() if self.m_prop_w_en.get() else "-2"
                h = self.m_prop_h.get() if self.m_prop_h_en.get() else "-2"
                if w != "-2" or h != "-2": scale_filter = f"scale={w}:{h}:flags=lanczos"
            elif self.m_res_mode.get() == 3:
                scale_filter = f"scale={self.m_exact_w.get()}:{self.m_exact_h.get()}:flags=lanczos"

            # -- 视频滤镜处理 (如果需要缩放或字幕，必须重新编码) --
            temp_sub_path = None
            if encoder != "copy":
                v_filters = []
                if scale_filter:
                    v_filters.append(scale_filter)
                
                if s_path:
                    temp_sub_name = f"temp_sub_m_{threading.get_ident()}.{'ass' if is_ass else 'srt'}"
                    temp_sub_path = os.path.join(out_dir, temp_sub_name)  # 临时字幕放在原输出目录，供 ffmpeg 读取
                    shutil.copy2(s_path, temp_sub_path)
                    
                    if is_ass:
                        v_filters.append(f"ass='{temp_sub_name}'")
                    else:
                        font = self.m_font_name.get().strip()
                        size = self.m_font_size.get()
                        outl = self.m_font_outline.get()
                        marv = self.m_font_marginv.get()
                        
                        if '/' in font or '\\' in font:
                            font = font.replace('\\', '/')
                            font_dir = os.path.dirname(font).replace(':', '\\:')
                            font_name_guess = os.path.splitext(os.path.basename(font))[0]
                            style = f"Fontname={font_name_guess},Fontsize={size},Outline={outl},MarginV={marv}"
                            v_filters.append(f"subtitles='{temp_sub_name}':fontsdir='{font_dir}':force_style='{style}'")
                        else:
                            style = f"Fontname={font},Fontsize={size},Outline={outl},MarginV={marv}"
                            v_filters.append(f"subtitles='{temp_sub_name}':force_style='{style}'")
                            
                if v_filters:
                    v_filter_chain = ",".join(v_filters)
                    fc_parts.append(f"[0:v:0]{v_filter_chain}[vout]")
                    v_out = "[vout]"

            # -- 复杂音频处理与互斥策略 --
            def get_vol_mult(vol_str):
                if "静音" in vol_str: return "0.0"
                
                # 提取用户输入的百分比数字
                pct_val = float(vol_str.replace("%", "").strip())
                
                if pct_val == 0: return "0.0"
                if pct_val == 100: return "1.0"
                
                # === 核心修改：声学心理感知对数映射 ===
                # 听觉规律：感知响度翻倍或减半，对应物理衰减/增益约 10dB
                # 1. 计算感知 dB 值：以 100% 为基准 (0dB)，根据输入比例计算对应 dB
                db_val = 10 * math.log2(pct_val / 100.0)
                
                # 2. 将 dB 值转换为 FFmpeg 真正需要的线性振幅倍数 (Amplitude Multiplier)
                # 物理公式：倍数 = 10 ^ (dB / 20)
                mult = 10 ** (db_val / 20.0)
                
                return f"{mult:.4f}"

            mode = self.m_audio_mode.get()
            vol_o = get_vol_mult(self.m_orig_vol.get())
            vol_v = get_vol_mult(self.m_voice_vol.get())
            vol_b = get_vol_mult(self.m_bgm_vol.get())

            has_a1 = bool(a1_path)
            has_a2 = bool(a2_path)
            num_ext_audio = int(has_a1) + int(has_a2)

            # 决定是否将原视频音轨加入混音器
            has_a0_in_filter = False
            if num_ext_audio > 0 and self.m_keep_orig_audio.get():
                has_a0_in_filter = True
                
            active_in_filter_count = int(has_a0_in_filter) + int(has_a1) + int(has_a2)

            # === 新增：获取视频精准时长以严格限制输出，防止时长增加 ===
            dur_v = self.get_video_duration(v_path)
            
            # === 新增：解析音频偏移与拉伸比例 ===
            offset_val = 0
            force_tempo = False
            tempo_str = ""
            
            if mode != 3: # 选项3(无损)开启时自动忽略偏移与拉伸
                try: offset_val = int(self.m_audio_offset.get().strip())
                except: pass
                force_tempo = self.m_force_tempo.get()
                
                # 若需要拉伸，通过 ffmpeg 的 atempo 级联滤镜计算速率
                if force_tempo and dur_v > 0:
                    dur_a = 0
                    if has_a1: dur_a = self.get_video_duration(a1_path)
                    elif has_a2: dur_a = self.get_video_duration(a2_path)
                    
                    if dur_a > 0:
                        ratio = dur_a / dur_v # 速率比，大于1加速(压缩时间)，小于1减速(拉伸时间)
                        atempos = []
                        temp_r = ratio
                        # atempo 单次极限是 0.5 到 2.0，超出需级联
                        while temp_r > 2.0: 
                            atempos.append("atempo=2.0")
                            temp_r /= 2.0
                        while temp_r < 0.5: 
                            atempos.append("atempo=0.5")
                            temp_r /= 0.5
                        atempos.append(f"atempo={temp_r:.4f}")
                        tempo_str = ",".join(atempos)

            if num_ext_audio > 0:
                # === 获取用户设定的目标 LUFS 标准 (默认 -24.0 广播标准) ===
                try: target_lufs = float(self.m_target_lufs.get())
                except: target_lufs = -24.0

                if active_in_filter_count == 1:
                    # 只有一条外部音轨
                    idx = f"{v_idx}:a:0" if has_a1 else f"{b_idx}:a:0"
                    vol = vol_v if has_a1 else vol_b
                    
                    if mode == 3: # 无损直接映射
                        a_out = idx
                    else:
                        a_chain = []
                        # 方案一：率先在源头切入独立标准化，带上设定的绝对标准 LUFS
                        if mode == 4: a_chain.append(f"loudnorm=I={target_lufs}:TP=-1.5:LRA=11") 
                        
                        a_chain.append(f"volume={vol}")
                        if offset_val > 0: a_chain.append(f"adelay={offset_val}|{offset_val}")
                        elif offset_val < 0: a_chain.append(f"atrim=start={abs(offset_val)/1000.0},asetpts=PTS-STARTPTS")
                        if force_tempo and tempo_str: a_chain.append(tempo_str)
                        
                        if mode == 2: a_chain.append("loudnorm") 
                        
                        a_filter = ",".join(a_chain)
                        fc_parts.append(f"[{idx}]{a_filter}[aout]")
                        a_out = "[aout]"

                else: # active_in_filter_count >= 2
                    mix_inputs = []
                    # 【核心修改：为每一条音轨建立独立的“标准化(指定LUFS) -> 对数百分比缩放 -> 偏移拉伸”微型加工流水线】
                    if has_a0_in_filter:
                        a0_chain = []
                        if mode == 4: a0_chain.append(f"loudnorm=I={target_lufs}:TP=-1.5:LRA=11")
                        a0_chain.append(f"volume={vol_o}")
                        fc_parts.append(f"[0:a:0]{','.join(a0_chain)}[a0_vol]")
                        mix_inputs.append("[a0_vol]")
                        
                    if has_a1:
                        a1_chain = []
                        if mode == 4: a1_chain.append(f"loudnorm=I={target_lufs}:TP=-1.5:LRA=11")
                        a1_chain.append(f"volume={vol_v}")
                        if offset_val > 0: a1_chain.append(f"adelay={offset_val}|{offset_val}")
                        elif offset_val < 0: a1_chain.append(f"atrim=start={abs(offset_val)/1000.0},asetpts=PTS-STARTPTS")
                        if force_tempo and tempo_str: a1_chain.append(tempo_str)
                        fc_parts.append(f"[{v_idx}:a:0]{','.join(a1_chain)}[a1_vol]")
                        mix_inputs.append("[a1_vol]")
                        
                    if has_a2:
                        a2_chain = []
                        if mode == 4: a2_chain.append(f"loudnorm=I={target_lufs}:TP=-1.5:LRA=11")
                        a2_chain.append(f"volume={vol_b}")
                        if offset_val > 0: a2_chain.append(f"adelay={offset_val}|{offset_val}")
                        elif offset_val < 0: a2_chain.append(f"atrim=start={abs(offset_val)/1000.0},asetpts=PTS-STARTPTS")
                        if force_tempo and tempo_str: a2_chain.append(tempo_str)
                        fc_parts.append(f"[{b_idx}:a:0]{','.join(a2_chain)}[a2_vol]")
                        mix_inputs.append("[a2_vol]")
                        
                    mix_str = "".join(mix_inputs)
                    
                    if mode == 2: # 自动 loudnorm (先混合所有声音，再对整体进行防爆音平衡)
                        fc_parts.append(f"{mix_str}amix=inputs={active_in_filter_count}:duration=longest:normalize=0[amix]")
                        fc_parts.append(f"[amix]loudnorm[aout]")
                        a_out = "[aout]"
                    else: # 模式 1 (纯手工) 或 模式 4 (单轨独立平衡后缩放)
                        # 因为前面已经做过标准化和按百分比的对数缩放，这里只需原样纯净混合即可
                        fc_parts.append(f"{mix_str}amix=inputs={active_in_filter_count}:duration=longest:normalize=0[aout]")
                        a_out = "[aout]"

            # === 最核心的修复：把误删的流映射与滤镜挂载代码补回来！ ===
            if fc_parts:
                cmd.extend(["-filter_complex", ";".join(fc_parts)])
                cmd.extend(["-map", v_out])
                if a_out: cmd.extend(["-map", a_out]) # 只有包含有效音频流时才映射
            else:
                # 没有任何滤镜介入，直接以原生流输入防报错
                cmd.extend(["-map", "0:v:0?"])
                if a_out: cmd.extend(["-map", a_out])

            # -- 视频编码逻辑 --
            cmd.extend(["-c:v", encoder])
            if encoder != "copy":
                if self.m_quality_mode.get() == 1:
                    cmd.extend(["-b:v", f"{self.m_bitrate.get()}k"])
                elif self.m_quality_mode.get() == 2:
                    q = self.m_crf.get()
                    if "libx26" in encoder: cmd.extend(["-crf", q])
                    elif "nvenc" in encoder: cmd.extend(["-cq", q])
                    elif "amf" in encoder: cmd.extend(["-rc", "cqp", "-qp_i", q, "-qp_p", q, "-qp_b", q])
                    elif "qsv" in encoder: cmd.extend(["-global_quality", q])
                    elif "videotoolbox" in encoder: cmd.extend(["-q:v", q])
                    else: cmd.extend(["-crf", q])
                elif self.m_quality_mode.get() == 3:
                    # 动态探测原视频流码率
                    orig_v_bitrate = self.get_video_stream_bitrate(v_path)
                    if orig_v_bitrate:
                        cmd.extend(["-b:v", orig_v_bitrate])
                    else:
                        cmd.extend(["-crf", "28"]) # 提取失败则回退默认
                elif self.m_quality_mode.get() == 4:
                    # === 智能限流核心逻辑 ===
                    try: target_br = int(self.m_max_bitrate.get())
                    except: target_br = 3000
                    orig_v_bitrate = self.get_video_stream_bitrate(v_path)
                    if orig_v_bitrate:
                        orig_br_kbps = int(orig_v_bitrate) // 1000
                        if orig_br_kbps > target_br:
                            cmd.extend(["-b:v", f"{target_br}k"])
                        else:
                            cmd.extend(["-b:v", orig_v_bitrate])
                    else:
                        cmd.extend(["-b:v", f"{target_br}k"])
                
                preset_val = self.m_preset.get()
                if encoder == "h264_amf":
                    amf_preset_map = {"fast": "speed", "medium": "balanced", "slow": "quality"}
                    cmd.extend(["-quality", amf_preset_map.get(preset_val, "balanced")])
                else:
                    cmd.extend(["-preset", preset_val])
                
                fps_val = self.m_fps_var.get()
                if fps_val != "保持原始":
                    cmd.extend(["-r", fps_val])

                threads_val = self.m_threads_var.get()
                if threads_val != "自动":
                    cmd.extend(["-threads", threads_val])
            
            # -- 音频编码策略逻辑 --
            if not a_out:
                pass # 如果没有输出音频（a_out为空），则彻底跳过音频编码参数配置
            elif active_in_filter_count == 1 and mode == 3:
                # 只有单条独立音轨，且选择了保持原始拷贝
                active_path = a1_path if has_a1 else (a2_path if has_a2 else "")
                # 智能拦截：WAV/FLAC等无损格式直封入视频会导致严重的兼容性爆音(不支持的IPCM)
                if active_path and active_path.lower().endswith(('.wav', '.flac', '.pcm')):
                    audio_br = self.m_audio_bitrate_var.get().strip()
                    cmd.extend(["-c:a", "aac", "-b:a", f"{audio_br}k"])
                else:
                    cmd.extend(["-c:a", "copy"])
            elif num_ext_audio > 0 or (num_ext_audio == 0 and not s_path):
                # 如果有多轨混合、有滤镜，或需要重新压缩防崩
                audio_br = self.m_audio_bitrate_var.get().strip()
                cmd.extend(["-c:a", "aac", "-b:a", f"{audio_br}k"])
            else:
                # 仅有视频+字幕，默认无损保留原视频音频
                cmd.extend(["-c:a", "copy"])
            # === 最强保险：利用 -t 参数严格强制视频在原始时长处停止，斩断因音频推后或超长产生的所有尾巴 ===
            if dur_v > 0 and (self.m_force_crop.get() or self.m_force_tempo.get()):
                cmd.extend(["-t", str(dur_v)])
                
            cmd.append(out_file)

            self.root.after(0, self.m_status_text.set, f"正在合并 ({i+1}/{total_files}): {v_filename}")
            self.root.after(0, self.m_progress_var.set, 0)

            total_duration_sec = 0

            try:
                # 必须指定 cwd 为 out_dir，以便 FFmpeg 能直接通过文件名识别并渲染刚才复制的临时字幕
                self.current_process = subprocess.Popen(
                    cmd, 
                    stderr=subprocess.PIPE, 
                    stdout=subprocess.PIPE, 
                    encoding='utf-8', 
                    errors='ignore',
                    creationflags=subprocess.CREATE_NO_WINDOW,
                    cwd=out_dir
                )

                for line in self.current_process.stderr:
                    if self.is_cancelled:
                        self.current_process.kill()
                        break
                    
                    if total_duration_sec == 0:
                        dur_match = self.re_duration.search(line)
                        if dur_match:
                            total_duration_sec = self.time_to_seconds(*dur_match.groups())

                    time_match = self.re_time.search(line)
                    if time_match and total_duration_sec > 0:
                        current_sec = self.time_to_seconds(*time_match.groups())
                        percent = min((current_sec / total_duration_sec) * 100, 100)
                        self.root.after(0, self.m_progress_var.set, percent)

                self.current_process.wait()

            except Exception as e:
                print(f"合并文件 {v_filename} 时发生异常: {e}")
                if self.current_process:
                    try:
                        self.current_process.kill()
                    except:
                        pass
            finally:
                # 销毁在输出目录里产生的临时字幕文件
                if temp_sub_path and os.path.exists(temp_sub_path):
                    try:
                        os.remove(temp_sub_path)
                    except:
                        pass

            if self.is_cancelled:
                if os.path.exists(out_file):
                    try:
                        os.remove(out_file)
                    except OSError:
                        pass
                break

        if self.is_cancelled:
            self.root.after(0, self.merge_reset_ui, "合并已中止！未完成的文件已清理。")
            if self.m_output_as_zip.get() and os.path.exists(actual_out_dir):
                shutil.rmtree(actual_out_dir, ignore_errors=True)
        else:
            # === 开始执行 Zip 打包逻辑 ===
            if self.m_output_as_zip.get():
                self.root.after(0, self.m_status_text.set, "音视频合并完毕，正在进行分卷打包...")
                try:
                    max_f = int(self.m_zip_max_var.get().strip())
                    processed_files = [f for f in os.listdir(actual_out_dir) if os.path.isfile(os.path.join(actual_out_dir, f))]
                    processed_files.sort()
                    num_zips = math.ceil(len(processed_files) / max_f)

                    for j in range(num_zips):
                        chunk = processed_files[j*max_f : (j+1)*max_f]
                        first_name = os.path.splitext(chunk[0])[0]
                        last_name = os.path.splitext(chunk[-1])[0]

                        if first_name == last_name:
                            zip_filename = f"{first_name}.zip"
                        else:
                            zip_filename = f"{first_name}_{last_name}.zip"

                        zip_path = os.path.join(out_dir, zip_filename)
                        
                        with zipfile.ZipFile(zip_path, 'w', compression=zipfile.ZIP_STORED) as zf:
                            for f in chunk:
                                file_path = os.path.join(actual_out_dir, f)
                                zf.write(file_path, f)

                    shutil.rmtree(actual_out_dir, ignore_errors=True)
                    
                except Exception as e:
                    self.root.after(0, self.merge_reset_ui, f"打包发生异常: {str(e)}", False)
                    return
                
            self.root.after(0, self.m_progress_var.set, 100)
            msg = "合并处理完毕！" if processed_count > 0 else "未发现能匹配音频或字幕的同名视频文件，已跳过。"
            self.root.after(0, self.merge_reset_ui, msg, processed_count > 0)

    def merge_reset_ui(self, message, success=False):
        self.is_merge_processing = False
        self.current_process = None
        self.btn_run_merge.config(state="normal")
        self.btn_stop_merge.config(state="disabled")
        self.m_status_text.set(message)
        if success:
            messagebox.showinfo("完成", message)
        else:
            messagebox.showwarning("提示", message)

# ================= 新增：视频拆分/合并 UI 与逻辑 =================
    def setup_split_merge_ui(self):
        # 1. 顶部模式选择
        self.frame_sm_action = ttk.Frame(self.tab_split_merge, padding=(10, 5))
        self.frame_sm_action.pack(fill="x")
        ttk.Radiobutton(self.frame_sm_action, text="模式：视频拆分", variable=self.sm_action, value=1, command=self.update_sm_ui_state).pack(side="left", padx=(0, 20))
        ttk.Radiobutton(self.frame_sm_action, text="模式：视频合并 (依文件名)", variable=self.sm_action, value=2, command=self.update_sm_ui_state).pack(side="left")

        # 2. 拆分专属面板
        self.frame_sm_split = ttk.LabelFrame(self.tab_split_merge, text="拆分输入输出设置", padding=10)
        ttk.Label(self.frame_sm_split, text="输入视频目录:").grid(row=0, column=0, sticky="e", pady=2)
        ttk.Entry(self.frame_sm_split, textvariable=self.sm_split_in).grid(row=0, column=1, sticky="we", padx=5)
        ttk.Button(self.frame_sm_split, text="浏览...", command=lambda: self.browse_dir(self.sm_split_in)).grid(row=0, column=2)

        ttk.Label(self.frame_sm_split, text="前半部分输出:").grid(row=1, column=0, sticky="e", pady=2)
        ttk.Entry(self.frame_sm_split, textvariable=self.sm_split_out1).grid(row=1, column=1, sticky="we", padx=5)
        ttk.Button(self.frame_sm_split, text="浏览...", command=lambda: self.browse_dir(self.sm_split_out1)).grid(row=1, column=2)

        ttk.Label(self.frame_sm_split, text="后半部分输出:").grid(row=2, column=0, sticky="e", pady=2)
        ttk.Entry(self.frame_sm_split, textvariable=self.sm_split_out2).grid(row=2, column=1, sticky="we", padx=5)
        ttk.Button(self.frame_sm_split, text="浏览...", command=lambda: self.browse_dir(self.sm_split_out2)).grid(row=2, column=2)

        # 新增：拆分计算方向选择
        f_split_dir = ttk.Frame(self.frame_sm_split)
        f_split_dir.grid(row=3, column=0, columnspan=3, sticky="w", pady=(5,0))
        ttk.Label(f_split_dir, text="计算方向:").pack(side="left", padx=(0, 5))
        ttk.Radiobutton(f_split_dir, text="正序 (从头开始计算)", variable=self.sm_split_dir, value=1).pack(side="left", padx=(0, 15))
        ttk.Radiobutton(f_split_dir, text="倒序 (从尾部往前推算)", variable=self.sm_split_dir, value=2).pack(side="left")

        # 原有的时间/百分比选择（下移到了 row=4）
        f_split_mode = ttk.Frame(self.frame_sm_split)
        f_split_mode.grid(row=4, column=0, columnspan=3, sticky="w", pady=(5,0))
        ttk.Radiobutton(f_split_mode, text="按时间节点拆分 (HH:MM:SS):", variable=self.sm_split_mode, value=1, command=self.update_sm_ui_state).pack(side="left")
        self.entry_sm_time = ttk.Entry(f_split_mode, textvariable=self.sm_split_time, width=10)
        self.entry_sm_time.pack(side="left", padx=(0, 20))
        ttk.Radiobutton(f_split_mode, text="按百分比拆分(%):", variable=self.sm_split_mode, value=2, command=self.update_sm_ui_state).pack(side="left")
        self.entry_sm_pct = ttk.Entry(f_split_mode, textvariable=self.sm_split_pct, width=8)
        self.entry_sm_pct.pack(side="left")
        self.frame_sm_split.columnconfigure(1, weight=1)

        # 3. 合并专属面板
        self.frame_sm_merge = ttk.LabelFrame(self.tab_split_merge, text="合并输入输出设置", padding=10)
        ttk.Label(self.frame_sm_merge, text="前半部分视频:").grid(row=0, column=0, sticky="e", pady=2)
        ttk.Entry(self.frame_sm_merge, textvariable=self.sm_merge_in1).grid(row=0, column=1, sticky="we", padx=5)
        ttk.Button(self.frame_sm_merge, text="浏览...", command=lambda: self.browse_dir(self.sm_merge_in1)).grid(row=0, column=2)

        ttk.Label(self.frame_sm_merge, text="后半部分视频:").grid(row=1, column=0, sticky="e", pady=2)
        ttk.Entry(self.frame_sm_merge, textvariable=self.sm_merge_in2).grid(row=1, column=1, sticky="we", padx=5)
        ttk.Button(self.frame_sm_merge, text="浏览...", command=lambda: self.browse_dir(self.sm_merge_in2)).grid(row=1, column=2)

        ttk.Label(self.frame_sm_merge, text="合并输出目录:").grid(row=2, column=0, sticky="e", pady=2)
        ttk.Entry(self.frame_sm_merge, textvariable=self.sm_merge_out).grid(row=2, column=1, sticky="we", padx=5)
        ttk.Button(self.frame_sm_merge, text="浏览...", command=lambda: self.browse_dir(self.sm_merge_out)).grid(row=2, column=2)
        self.frame_sm_merge.columnconfigure(1, weight=1)

        # 4. 全局共同设置面板
        self.frame_sm_common = ttk.LabelFrame(self.tab_split_merge, text="全局视音频参数设定", padding=10)
        self.frame_sm_common.pack(fill="both", expand=True, padx=10, pady=5)
        
        f_top_opts = ttk.Frame(self.frame_sm_common)
        f_top_opts.pack(fill="x", pady=(0, 5))
        ttk.Checkbutton(f_top_opts, text="保留音频", variable=self.sm_keep_audio).pack(side="left", padx=(0, 20))
        ttk.Checkbutton(f_top_opts, text="全部保留原始视频参数 (仅极速流复制拆分/合并, 不重新编码)", variable=self.sm_copy_stream, command=self.update_sm_ui_state).pack(side="left")

        # 右侧紧凑布局移植
        lf_right = ttk.Frame(self.frame_sm_common)
        lf_right.pack(fill="both", expand=True, pady=(5,0))

        # 封装格式与视频编码 (同行)
        f_fmt_enc_sm = ttk.Frame(lf_right)
        f_fmt_enc_sm.grid(row=0, column=0, columnspan=5, sticky="we", pady=2)
        ttk.Label(f_fmt_enc_sm, text="格式:").pack(side="left")
        self.sm_cb_fmt = ttk.Combobox(f_fmt_enc_sm, textvariable=self.sm_fmt_var, values=self.m_fmt_options, state="readonly", width=6)
        self.sm_cb_fmt.pack(side="left", padx=(0, 10))
        
        ttk.Label(f_fmt_enc_sm, text="编码:").pack(side="left")
        self.sm_cb_vcodec = ttk.Combobox(f_fmt_enc_sm, textvariable=self.sm_codec_var, values=["保持原始", "H.264", "H.265"], state="readonly", width=8)
        self.sm_cb_vcodec.pack(side="left")

        # 硬件加速引擎
        f_hw_sm = ttk.Frame(lf_right)
        f_hw_sm.grid(row=1, column=0, columnspan=5, sticky="we", pady=2)
        ttk.Label(f_hw_sm, text="硬件加速:").pack(side="left")
        self.sm_cb_enc = ttk.Combobox(f_hw_sm, textvariable=self.sm_encoder_var, values=list(self.encoder_map.keys()), state="readonly", width=15)
        self.sm_cb_enc.pack(side="left")

        ttk.Separator(lf_right, orient='horizontal').grid(row=2, column=0, columnspan=5, sticky="we", pady=5)

        # === 替换开始：极限横向排布 ===
        # 1. 独立分辨率设置 (单行横向排布)
        frame_res_sm = ttk.Frame(lf_right)
        frame_res_sm.grid(row=3, column=0, columnspan=2, sticky="we", pady=5)

        self.sm_rb_res1 = ttk.Radiobutton(frame_res_sm, text="保持原分辨率", variable=self.sm_res_mode, value=1, command=self.update_sm_res_ui)
        self.sm_rb_res1.pack(side="left", padx=(0, 15))

        self.sm_rb_res2 = ttk.Radiobutton(frame_res_sm, text="缩放:", variable=self.sm_res_mode, value=2, command=self.update_sm_res_ui)
        self.sm_rb_res2.pack(side="left")
        self.sm_cb_prop_w = ttk.Checkbutton(frame_res_sm, text="宽:", variable=self.sm_prop_w_en, command=self.on_sm_prop_w_check)
        self.sm_cb_prop_w.pack(side="left")
        self.sm_entry_prop_w = ttk.Entry(frame_res_sm, textvariable=self.sm_prop_w, width=5)
        self.sm_entry_prop_w.pack(side="left", padx=(0, 5))
        self.sm_cb_prop_h = ttk.Checkbutton(frame_res_sm, text="高:", variable=self.sm_prop_h_en, command=self.on_sm_prop_h_check)
        self.sm_cb_prop_h.pack(side="left")
        self.sm_entry_prop_h = ttk.Entry(frame_res_sm, textvariable=self.sm_prop_h, width=5)
        self.sm_entry_prop_h.pack(side="left", padx=(0, 15))

        self.sm_rb_res3 = ttk.Radiobutton(frame_res_sm, text="指定:", variable=self.sm_res_mode, value=3, command=self.update_sm_res_ui)
        self.sm_rb_res3.pack(side="left")
        ttk.Label(frame_res_sm, text="宽:").pack(side="left")
        self.sm_entry_exact_w = ttk.Entry(frame_res_sm, textvariable=self.sm_exact_w, width=5)
        self.sm_entry_exact_w.pack(side="left", padx=(0, 5))
        ttk.Label(frame_res_sm, text="高:").pack(side="left")
        self.sm_entry_exact_h = ttk.Entry(frame_res_sm, textvariable=self.sm_exact_h, width=5)
        self.sm_entry_exact_h.pack(side="left")
        
        ttk.Separator(lf_right, orient='horizontal').grid(row=4, column=0, columnspan=2, sticky="we", pady=5)

        # 2. 码率、帧率、预设、线程 (单行横向排布)
        frame_q_sm = ttk.Frame(lf_right)
        frame_q_sm.grid(row=5, column=0, columnspan=2, sticky="we", pady=(0, 5))

        self.sm_rb_q1 = ttk.Radiobutton(frame_q_sm, text="固定:", variable=self.sm_quality_mode, value=1)
        self.sm_rb_q1.pack(side="left")
        self.sm_entry_br = ttk.Entry(frame_q_sm, textvariable=self.sm_bitrate, width=5)
        self.sm_entry_br.pack(side="left", padx=(0, 2))

        self.sm_rb_q4 = ttk.Radiobutton(frame_q_sm, text="上限:", variable=self.sm_quality_mode, value=4)
        self.sm_rb_q4.pack(side="left")
        self.sm_entry_max_br = ttk.Entry(frame_q_sm, textvariable=self.sm_max_bitrate, width=5)
        self.sm_entry_max_br.pack(side="left", padx=(0, 2))

        self.sm_rb_q2 = ttk.Radiobutton(frame_q_sm, text="CRF:", variable=self.sm_quality_mode, value=2)
        self.sm_rb_q2.pack(side="left")
        self.sm_entry_crf = ttk.Entry(frame_q_sm, textvariable=self.sm_crf, width=3)
        self.sm_entry_crf.pack(side="left", padx=(0, 2))

        self.sm_rb_q3 = ttk.Radiobutton(frame_q_sm, text="保原码率", variable=self.sm_quality_mode, value=3)
        self.sm_rb_q3.pack(side="left", padx=(0, 5))

        ttk.Label(frame_q_sm, text="FPS:").pack(side="left")
        self.sm_cb_fps = ttk.Combobox(frame_q_sm, textvariable=self.sm_fps_var, values=self.fps_options, state="readonly", width=4)
        self.sm_cb_fps.pack(side="left", padx=(0, 5))

        ttk.Label(frame_q_sm, text="预设:").pack(side="left")
        self.sm_cb_preset = ttk.Combobox(frame_q_sm, textvariable=self.sm_preset, values=["fast", "medium", "slow"], state="readonly", width=6)
        self.sm_cb_preset.pack(side="left", padx=(0, 5))

        ttk.Label(frame_q_sm, text="线程:").pack(side="left")
        self.sm_cb_threads = ttk.Combobox(frame_q_sm, textvariable=self.sm_threads_var, values=self.threads_options, state="readonly", width=3)
        self.sm_cb_threads.pack(side="left", padx=(0, 5))

        ttk.Label(frame_q_sm, text="音码:").pack(side="left")
        self.sm_cb_audio_br = ttk.Combobox(frame_q_sm, textvariable=self.sm_audio_bitrate_var, values=["保持原始", "128", "192", "256", "320"], width=6)
        self.sm_cb_audio_br.pack(side="left")

        # 5. 底部操作区
        frame_bottom_sm = ttk.Frame(self.tab_split_merge, padding=(10, 10))
        frame_bottom_sm.pack(fill="x", side="bottom", pady=5)
        frame_bottom_sm.columnconfigure(1, weight=1)
        self.lbl_sm_status = ttk.Label(frame_bottom_sm, textvariable=self.sm_status_text)
        self.lbl_sm_status.grid(row=0, column=0, sticky="e", padx=(0, 5))
        self.sm_pb = ttk.Progressbar(frame_bottom_sm, orient="horizontal", mode="determinate", variable=self.sm_progress_var)
        self.sm_pb.grid(row=0, column=1, sticky="we", padx=10)
        self.btn_run_sm = ttk.Button(frame_bottom_sm, text="开始执行", width=12, command=self.start_sm)
        self.btn_run_sm.grid(row=0, column=2, padx=5)
        self.btn_stop_sm = ttk.Button(frame_bottom_sm, text="停止", width=12, command=self.stop_sm, state="disabled")
        self.btn_stop_sm.grid(row=0, column=3)

        self.update_sm_ui_state()

    def update_sm_ui_state(self, *args):
        if self.sm_action.get() == 1:
            self.frame_sm_split.pack(fill="x", padx=10, pady=5, after=self.frame_sm_action)
            self.frame_sm_merge.pack_forget()
        else:
            self.frame_sm_merge.pack(fill="x", padx=10, pady=5, after=self.frame_sm_action)
            self.frame_sm_split.pack_forget()

        if self.sm_split_mode.get() == 1:
            self.entry_sm_time.config(state="normal")
            self.entry_sm_pct.config(state="disabled")
        else:
            self.entry_sm_time.config(state="disabled")
            self.entry_sm_pct.config(state="normal")

        # 处理无损极速复制与视频参数重编码的互斥
        t_state_c = "disabled" if self.sm_copy_stream.get() else "readonly"
        t_state_e = "disabled" if self.sm_copy_stream.get() else "normal"
            
        self.sm_cb_fmt.config(state=t_state_c)
        self.sm_cb_vcodec.config(state=t_state_c) 
        self.sm_cb_enc.config(state=t_state_c)
        self.sm_rb_res1.config(state=t_state_e)
        self.sm_rb_res2.config(state=t_state_e)
        self.sm_rb_res3.config(state=t_state_e)
        self.sm_rb_q1.config(state=t_state_e)
        self.sm_rb_q2.config(state=t_state_e)
        self.sm_rb_q3.config(state=t_state_e)
        self.sm_rb_q4.config(state=t_state_e) # 新增：限码单选框互斥
        self.sm_entry_br.config(state=t_state_e)
        self.sm_entry_crf.config(state=t_state_e)
        self.sm_entry_max_br.config(state=t_state_e) # 新增：限码输入框互斥
        self.sm_cb_fps.config(state=t_state_c)
        self.sm_cb_preset.config(state=t_state_c)
        self.sm_cb_threads.config(state=t_state_c)
        self.sm_cb_audio_br.config(state=t_state_c) 
        self.update_sm_res_ui()

    def on_sm_prop_w_check(self):
        if self.sm_prop_w_en.get(): self.sm_prop_h_en.set(False)
        self.update_sm_res_ui()

    def on_sm_prop_h_check(self):
        if self.sm_prop_h_en.get(): self.sm_prop_w_en.set(False)
        self.update_sm_res_ui()

    def update_sm_res_ui(self, *args):
        if self.sm_copy_stream.get():
            self.sm_cb_prop_w.state(['disabled'])
            self.sm_cb_prop_h.state(['disabled'])
            self.sm_entry_prop_w.state(['disabled'])
            self.sm_entry_prop_h.state(['disabled'])
            self.sm_entry_exact_w.state(['disabled'])
            self.sm_entry_exact_h.state(['disabled'])
            return

        mode = self.sm_res_mode.get()
        if mode == 1:
            self.sm_cb_prop_w.state(['disabled'])
            self.sm_cb_prop_h.state(['disabled'])
            self.sm_entry_prop_w.state(['disabled'])
            self.sm_entry_prop_h.state(['disabled'])
            self.sm_entry_exact_w.state(['disabled'])
            self.sm_entry_exact_h.state(['disabled'])
        elif mode == 2:
            self.sm_cb_prop_w.state(['!disabled'])
            self.sm_cb_prop_h.state(['!disabled'])
            self.sm_entry_exact_w.state(['disabled'])
            self.sm_entry_exact_h.state(['disabled'])
            if self.sm_prop_w_en.get():
                self.sm_entry_prop_w.state(['!disabled'])
                self.sm_entry_prop_h.state(['disabled'])
            elif self.sm_prop_h_en.get():
                self.sm_entry_prop_h.state(['!disabled'])
                self.sm_entry_prop_w.state(['disabled'])
        elif mode == 3:
            self.sm_cb_prop_w.state(['disabled'])
            self.sm_cb_prop_h.state(['disabled'])
            self.sm_entry_prop_w.state(['disabled'])
            self.sm_entry_prop_h.state(['disabled'])
            self.sm_entry_exact_w.state(['!disabled'])
            self.sm_entry_exact_h.state(['!disabled'])

    def parse_time_str_to_sec(self, time_str):
        try:
            parts = time_str.split(':')
            if len(parts) == 3: return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
            elif len(parts) == 2: return int(parts[0]) * 60 + float(parts[1])
            else: return float(parts[0])
        except: return -1.0

    def get_video_codec(self, filepath):
        """核心辅助：智能获取视频流真实的编码格式"""
        try:
            cmd = [self.ffprobe_bin, '-v', 'quiet', '-print_format', 'json', '-show_streams', '-select_streams', 'v:0', filepath]
            result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, encoding='utf-8', errors='ignore', creationflags=subprocess.CREATE_NO_WINDOW)
            data = json.loads(result.stdout)
            if 'streams' in data and len(data['streams']) > 0:
                codec_name = data['streams'][0].get('codec_name', '').lower()
                if codec_name in ['hevc', 'h265']:
                    return "H.265"
        except: pass
        return "H.264" # 探测失败或其它格式时，默认回退到兼容性最好的 H.264

    def get_video_duration(self, filepath):
        try:
            cmd = [self.ffprobe_bin, '-v', 'quiet', '-print_format', 'json', '-show_format', filepath]
            result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, encoding='utf-8', errors='ignore', creationflags=subprocess.CREATE_NO_WINDOW)
            data = json.loads(result.stdout)
            return float(data['format']['duration'])
        except: return 0.0

    def start_sm(self):
        if self.sm_action.get() == 1:
            if not all([self.sm_split_in.get(), self.sm_split_out1.get(), self.sm_split_out2.get()]):
                messagebox.showerror("错误", "请完善输入和输出目录设置！")
                return
        else:
            if not all([self.sm_merge_in1.get(), self.sm_merge_in2.get(), self.sm_merge_out.get()]):
                messagebox.showerror("错误", "请完善输入和输出目录设置！")
                return

        if not os.path.exists(self.ffmpeg_bin):
            messagebox.showerror("环境缺失", f"找不到 {self.ffmpeg_bin}！")
            return

        self.is_sm_processing = True
        self.is_cancelled = False
        self.btn_run_sm.config(state="disabled")
        self.btn_stop_sm.config(state="normal")
        self.sm_progress_var.set(0)
        
        threading.Thread(target=self.process_sm_thread, daemon=True).start()

    def stop_sm(self):
        if self.is_sm_processing:
            self.is_cancelled = True
            self.sm_status_text.set("正在中止处理，请稍候...")
            self.btn_stop_sm.config(state="disabled")
            if self.current_process:
                try: self.current_process.kill()
                except: pass

    def build_sm_codec_args(self, cmd, in_file):
        is_copy = self.sm_copy_stream.get()
        
        vcodec = self.sm_codec_var.get()
        hw_choice = self.sm_encoder_var.get()
        
        if is_copy or hw_choice == "纯转封装 (极速)":
            encoder = "copy"
        else:
            if vcodec == "保持原始":
                vcodec = self.get_video_codec(in_file) # 动态探测！
                
            if vcodec == "H.265":
                if "NVIDIA" in hw_choice: encoder = "hevc_nvenc"
                elif "AMD" in hw_choice: encoder = "hevc_amf"
                elif "Intel" in hw_choice: encoder = "hevc_qsv"
                elif "Apple" in hw_choice: encoder = "hevc_videotoolbox"
                else: encoder = "libx265"
            else: # 默认 H.264
                if "NVIDIA" in hw_choice: encoder = "h264_nvenc"
                elif "AMD" in hw_choice: encoder = "h264_amf"
                elif "Intel" in hw_choice: encoder = "h264_qsv"
                elif "Apple" in hw_choice: encoder = "h264_videotoolbox"
                else: encoder = "libx264"

        if encoder == "copy":
            cmd.extend(["-c:v", "copy"])
        else:
            cmd.extend(["-c:v", encoder])
            
            # 分辨率
            res_mode = self.sm_res_mode.get()
            scale_filter = ""
            if res_mode == 2:
                w = self.sm_prop_w.get() if self.sm_prop_w_en.get() else "-2"
                h = self.sm_prop_h.get() if self.sm_prop_h_en.get() else "-2"
                if w != "-2" or h != "-2": scale_filter = f"scale={w}:{h}:flags=lanczos"
            elif res_mode == 3:
                scale_filter = f"scale={self.sm_exact_w.get()}:{self.sm_exact_h.get()}:flags=lanczos"
            if scale_filter: cmd.extend(["-vf", scale_filter])
            
            # 质量/码率
            qm = self.sm_quality_mode.get()
            if qm == 1: cmd.extend(["-b:v", f"{self.sm_bitrate.get()}k"])
            elif qm == 2:
                q = self.sm_crf.get()
                if "libx26" in encoder: cmd.extend(["-crf", q])
                elif "nvenc" in encoder: cmd.extend(["-cq", q])
                elif "amf" in encoder: cmd.extend(["-rc", "cqp", "-qp_i", q, "-qp_p", q, "-qp_b", q])
                elif "qsv" in encoder: cmd.extend(["-global_quality", q])
                elif "videotoolbox" in encoder: cmd.extend(["-q:v", q])
                else: cmd.extend(["-crf", q])
            elif qm == 3:
                orig_v_bitrate = self.get_video_stream_bitrate(in_file)
                if orig_v_bitrate: cmd.extend(["-b:v", orig_v_bitrate])
                else: cmd.extend(["-crf", "28"])
            elif qm == 4:
                # === 智能限流核心逻辑 ===
                try: target_br = int(self.sm_max_bitrate.get())
                except: target_br = 3000
                orig_v_bitrate = self.get_video_stream_bitrate(in_file)
                if orig_v_bitrate:
                    orig_br_kbps = int(orig_v_bitrate) // 1000
                    if orig_br_kbps > target_br:
                        cmd.extend(["-b:v", f"{target_br}k"])
                    else:
                        cmd.extend(["-b:v", orig_v_bitrate])
                else:
                    cmd.extend(["-b:v", f"{target_br}k"])
                
            # 帧率与预设
            if self.sm_fps_var.get() != "保持原始": cmd.extend(["-r", self.sm_fps_var.get()])
            preset_val = self.sm_preset.get()
            if encoder == "h264_amf":
                cmd.extend(["-quality", {"fast":"speed", "medium":"balanced", "slow":"quality"}.get(preset_val, "balanced")])
            else: cmd.extend(["-preset", preset_val])
            if self.sm_threads_var.get() != "自动": cmd.extend(["-threads", self.sm_threads_var.get()])
            
        # 音频
        if not self.sm_keep_audio.get():
            cmd.extend(["-an"])
        elif is_copy:
            cmd.extend(["-c:a", "copy"])
        else:
            audio_br = self.sm_audio_bitrate_var.get().strip()
            if audio_br == "保持原始":
                orig_a_br = self.get_audio_stream_bitrate(in_file)
                if orig_a_br:
                    cmd.extend(["-c:a", "aac", "-b:a", orig_a_br])
                else:
                    cmd.extend(["-c:a", "aac", "-b:a", "192k"]) # 兜底
            else:
                cmd.extend(["-c:a", "aac", "-b:a", f"{audio_br}k"])

    def run_ffmpeg_sm(self, cmd, label):
        self.root.after(0, self.sm_status_text.set, f"正在处理: {label}")
        self.root.after(0, self.sm_progress_var.set, 0)
        total_duration_sec = 0
        try:
            self.current_process = subprocess.Popen(cmd, stderr=subprocess.PIPE, stdout=subprocess.PIPE, encoding='utf-8', errors='ignore', creationflags=subprocess.CREATE_NO_WINDOW)
            for line in self.current_process.stderr:
                if self.is_cancelled:
                    self.current_process.kill()
                    break
                if total_duration_sec == 0:
                    dur_match = self.re_duration.search(line)
                    if dur_match: total_duration_sec = self.time_to_seconds(*dur_match.groups())
                time_match = self.re_time.search(line)
                if time_match and total_duration_sec > 0:
                    current_sec = self.time_to_seconds(*time_match.groups())
                    percent = min((current_sec / total_duration_sec) * 100, 100)
                    self.root.after(0, self.sm_progress_var.set, percent)
            self.current_process.wait()
        except Exception as e:
            if self.current_process:
                try: self.current_process.kill()
                except: pass

    def process_sm_thread(self):
        action = self.sm_action.get()
        out_fmt = self.sm_fmt_var.get()
        is_copy = self.sm_copy_stream.get()
        processed_count = 0

        try:
            if action == 1: # === 拆分逻辑 ===
                in_dir = self.sm_split_in.get().strip()
                out1_dir, out2_dir = self.sm_split_out1.get().strip(), self.sm_split_out2.get().strip()
                os.makedirs(out1_dir, exist_ok=True); os.makedirs(out2_dir, exist_ok=True)
                files = [f for f in os.listdir(in_dir) if os.path.splitext(f)[1].lower() in self.supported_exts]
                
                for f_name in files:
                    if self.is_cancelled: break
                    base_name, ext = os.path.splitext(f_name)
                    in_path = os.path.join(in_dir, f_name)
                    dur = self.get_video_duration(in_path)
                    
                    if self.sm_split_mode.get() == 1:
                        split_sec = self.parse_time_str_to_sec(self.sm_split_time.get())
                    else:
                        pct = float(self.sm_split_pct.get()) / 100.0
                        split_sec = dur * pct
                    
                    # === 核心逻辑：若是倒序，从总时长中减去设定时间 ===
                    if self.sm_split_dir.get() == 2:
                        split_sec = dur - split_sec
                        
                    # 严密防呆：如果视频比倒扣的时间还短，或者解析出负数，则安全跳过不报错
                    if split_sec <= 0 or dur <= 0 or split_sec >= dur: continue
                    out_ext = ext if is_copy or out_fmt == "保持原格式" else "." + out_fmt.lower()
                    
                    # 前半部分
                    out1_path = os.path.join(out1_dir, base_name + out_ext)
                    cmd1 = [self.ffmpeg_bin, "-y", "-i", in_path, "-t", str(split_sec)]
                    self.build_sm_codec_args(cmd1, in_path)
                    cmd1.append(out1_path)
                    self.run_ffmpeg_sm(cmd1, f_name + " (前半段)")
                    if self.is_cancelled: break
                    
                    # 后半部分
                    out2_path = os.path.join(out2_dir, base_name + out_ext)
                    cmd2 = [self.ffmpeg_bin, "-y", "-ss", str(split_sec), "-i", in_path]
                    self.build_sm_codec_args(cmd2, in_path)
                    cmd2.append(out2_path)
                    self.run_ffmpeg_sm(cmd2, f_name + " (后半段)")
                    processed_count += 1
            
            else: # === 合并逻辑 ===
                in1_dir = self.sm_merge_in1.get().strip()
                in2_dir = self.sm_merge_in2.get().strip()
                out_dir = self.sm_merge_out.get().strip()
                os.makedirs(out_dir, exist_ok=True)
                files = [f for f in os.listdir(in1_dir) if os.path.splitext(f)[1].lower() in self.supported_exts]
                
                for f_name in files:
                    if self.is_cancelled: break
                    base_name, ext = os.path.splitext(f_name)
                    in1_path = os.path.join(in1_dir, f_name)
                    in2_path = os.path.join(in2_dir, f_name)
                    
                    if not os.path.exists(in2_path): continue # 没有对应后半段
                    out_ext = ext if is_copy or out_fmt == "保持原格式" else "." + out_fmt.lower()
                    out_path = os.path.join(out_dir, base_name + out_ext)
                    
                    list_txt = os.path.join(out_dir, f"temp_list_{threading.get_ident()}.txt")
                    with open(list_txt, "w", encoding="utf-8") as f:
                        f.write(f"file '{in1_path}'\nfile '{in2_path}'\n")
                        
                    cmd = [self.ffmpeg_bin, "-y", "-f", "concat", "-safe", "0", "-i", list_txt]
                    self.build_sm_codec_args(cmd, in1_path)
                    cmd.append(out_path)
                    
                    self.run_ffmpeg_sm(cmd, f_name + " (合并)")
                    if os.path.exists(list_txt): os.remove(list_txt)
                    processed_count += 1

        finally:
            if self.is_cancelled:
                self.root.after(0, self.sm_reset_ui, "处理已中止！")
            else:
                self.root.after(0, self.sm_progress_var.set, 100)
                msg = "拆分/合并处理完毕！" if processed_count > 0 else "未发现符合条件的文件（检查文件是否成对存在等），已跳过。"
                self.root.after(0, self.sm_reset_ui, msg, processed_count > 0)

    def sm_reset_ui(self, message, success=False):
        self.is_sm_processing = False
        self.current_process = None
        self.btn_run_sm.config(state="normal")
        self.btn_stop_sm.config(state="disabled")
        self.sm_status_text.set(message)
        if success: messagebox.showinfo("完成", message)
        else: messagebox.showwarning("提示", message)
        if not self.rv_rules: return True
        matched_any = False
        matched_all = True
        for rule in self.rv_rules:
            field_key = rule['field']
            if field_key == "字幕文本": field_key = "字幕文本 (Text)" 
            elif field_key == "样式/角色(仅ASS)": field_key = "全部列综合"
            target = field_data.get(field_key, "")
            is_match = False
            if rule['mode'] == '包含': is_match = rule['val'] in target
            elif rule['mode'] == '等于': is_match = rule['val'] == target
            elif rule['mode'] == '正则匹配':
                try: 
                    # 【核心修复】加入 re.DOTALL，让 .* 能够跨越 SRT 中的换行符，实现真正的“只要包含就处理”
                    if re.search(rule['val'], target, re.DOTALL): is_match = True
                except: pass
            if is_match: matched_any = True
            else: matched_all = False
        return matched_any if self.rv_rule_logic.get() == 1 else matched_all

if __name__ == "__main__":
    root = tk.Tk()
    app = FFmpegUltimateTool(root)
    root.mainloop()