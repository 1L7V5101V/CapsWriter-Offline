#!/usr/bin/env node
/**
 * wasapi_helper.cjs - WASAPI Loopback 捕获助手（Node.js）
 *
 * 使用 native-recorder-nodejs 原生模块捕获系统音频输出。
 * 通过 stdout 输出 PCM int16 数据。
 *
 * 用法：
 *   node wasapi_helper.cjs --list              # 枚举设备
 *   node wasapi_helper.cjs --capture [--device N]  # 捕获音频
 */
"use strict";

const path = require("path");

// native-recorder-nodejs 模块路径（指向 OpenCode 项目的安装位置）
const OPENCODE_NM_BASE = "D:/.Projects/OpenCode/node_modules/.bun/";

function findNativeModule() {
  const fs = require("fs");
  const entries = fs.readdirSync(OPENCODE_NM_BASE);
  const pkg = entries.find((d) => d.startsWith("native-recorder-nodejs"));
  if (!pkg) throw new Error("native-recorder-nodejs not found in " + OPENCODE_NM_BASE);
  return path.join(OPENCODE_NM_BASE, pkg, "node_modules", "native-recorder-nodejs");
}

const nativePath = findNativeModule();
const { AudioRecorder, nativeBindings } = require(nativePath);

const args = process.argv.slice(2);

// ==================== 枚举模式 ====================
if (args.includes("--list")) {
  try {
    const devices = AudioRecorder.getDevices("output");
    const defaultId = nativeBindings.AudioController.getDefaultOutputDeviceId
      ? nativeBindings.AudioController.getDefaultOutputDeviceId()
      : (devices.length > 0 ? devices[0].id : null);

    for (const dev of devices) {
      const result = {
        idx: dev.id,
        id: dev.id,
        name: dev.name,
        default: dev.id === defaultId,
      };
      console.log(JSON.stringify(result));
    }
  } catch (e) {
    console.error("Enumeration error:", e.message);
    process.exit(1);
  }
  process.exit(0);
}

// ==================== 捕获模式 ====================
if (args.includes("--capture")) {
  let deviceIdx = null;
  const idxArg = args.indexOf("--device");
  if (idxArg >= 0 && idxArg + 1 < args.length) {
    deviceIdx = parseInt(args[idxArg + 1], 10);
  }

  const recorder = new AudioRecorder();
  let headerSent = false;
  let dataCallback = null;

  recorder.on("data", (chunk) => {
    // chunk is Buffer (int16 PCM)
    if (!headerSent) {
      // 发送头信息
      const format = {
        sampleRate: 48000,
        channels: 1,
        format: "int16",
      };
      process.stdout.write(JSON.stringify(format) + "\n");
      headerSent = true;
    }

    // int16 PCM 数据直接写入 stdout
    process.stdout.write(chunk);
  });

  recorder.on("error", (err) => {
    console.error("Capture error:", err.message);
    process.exit(1);
  });

  // 获取设备列表（使用 AudioRecorder 静态方法）
  const outputs = AudioRecorder.getDevices("output");
  let defaultId = null;
  try {
    defaultId = nativeBindings.AudioController.getDefaultOutputDeviceId();
  } catch (e) { /* ignore */ }

  let targetDevice;
  if (deviceIdx !== null && deviceIdx >= 0 && deviceIdx < outputs.length) {
    targetDevice = outputs[deviceIdx];
  } else {
    targetDevice = outputs.find((d) => d.id === defaultId) || outputs[0];
  }

  if (!targetDevice) {
    console.error("No output device found");
    process.exit(1);
  }

  // 开始捕获
  recorder.start({
    deviceType: "output",
    deviceId: targetDevice.id,
  });

  // 保持进程存活直到 stdin 关闭
  process.stdin.on("end", () => {
    recorder.stop();
    process.exit(0);
  });

  // 父进程退出时停止
  process.on("SIGTERM", () => {
    recorder.stop();
    process.exit(0);
  });
}

// 如果没有参数，打印用法
if (!args.length) {
  console.log("Usage: wasapi_helper.cjs --list | --capture [--device N]");
}
