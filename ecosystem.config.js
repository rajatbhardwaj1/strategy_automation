// PM2 process definitions for the SIP strategy automation.
//
// Both apps are long-running Python processes and MUST run with the repo root
// as cwd: every path in the code is relative to it ("src/config.yaml",
// "logs/main.log"). The interpreter is the project virtualenv, so pm2 does not
// need the venv to be activated in the shell.
//
//   pm2 start ecosystem.config.js
//   pm2 logs strategy-scheduler
//
// NOTE: main.py schedules on *local* time (the `schedule` library has no
// timezone support), so the box must be on IST — see README "Deploying on EC2".
const path = require("path");

const ROOT = __dirname;
const PYTHON = path.join(ROOT, "venv", "bin", "python");

const common = {
  cwd: ROOT,
  interpreter: PYTHON,
  autorestart: true,
  max_restarts: 10,
  restart_delay: 10000,
  max_memory_restart: "500M",
  time: true,
  env: {
    PYTHONUNBUFFERED: "1",
    TZ: "Asia/Kolkata",
  },
};

module.exports = {
  apps: [
    {
      ...common,
      name: "strategy-scheduler",
      script: "main.py",
      out_file: path.join(ROOT, "logs", "pm2-scheduler-out.log"),
      error_file: path.join(ROOT, "logs", "pm2-scheduler-err.log"),
    },
    {
      ...common,
      name: "telegram-bot",
      script: "run_bot.py",
      out_file: path.join(ROOT, "logs", "pm2-bot-out.log"),
      error_file: path.join(ROOT, "logs", "pm2-bot-err.log"),
    },
  ],
};
