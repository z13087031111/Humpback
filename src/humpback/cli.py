"""Command line: ``humpback keystore create | info | check``.

Secrets are read with ``getpass`` (no echo, never from arguments or environment variables),
so they do not end up in shell history, process lists or logs, and no AI assistant sees them.
"""

from __future__ import annotations

import argparse
import getpass
import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

from humpback.domain.models import Venue
from humpback.signer import keystore

Prompt = Callable[[str], str]


def _say(text: str) -> None:
    sys.stdout.write(text + "\n")


def _git_tracks(path: Path) -> bool:
    """True if ``path`` would be committed (inside a git work tree and not ignored)."""
    try:
        inside = subprocess.run(  # noqa: S603
            ["git", "-C", str(path.parent), "rev-parse", "--is-inside-work-tree"],  # noqa: S607
            capture_output=True,
            text=True,
            check=False,
        )
        if inside.stdout.strip() != "true":
            return False
        ignored = subprocess.run(  # noqa: S603
            ["git", "-C", str(path.parent), "check-ignore", "-q", str(path)],  # noqa: S607
            check=False,
        )
    except FileNotFoundError:
        return False
    return ignored.returncode != 0


def cmd_create(args: argparse.Namespace, prompt: Prompt) -> int:
    venue = Venue(args.venue)
    out = Path(args.out).expanduser().resolve()
    if _git_tracks(out):
        _say(f"拒绝：{out} 位于 git 仓库内且未被忽略，可能会被提交。请换到 var/keys/ 或仓库外。")
        return 2
    _say(f"为 {venue.value} 创建加密密钥文件：{out}")
    _say("只放代理（API）密钥，绝不要放主钱包私钥。输入时屏幕不会显示。")
    key = keystore.parse_private_key(prompt("代理私钥（hex）: "), venue)
    passphrase = prompt(f"设置口令（至少 {keystore.MIN_PASSPHRASE} 个字符）: ")
    keystore.check_passphrase(passphrase)
    if prompt("再次输入口令: ") != passphrase:
        _say("两次口令不一致，未写入任何文件。")
        return 2
    info = keystore.create(out, key, passphrase, venue=venue, label=args.label)
    _print_info(info)
    _say("完成。请核对上面的地址与交易所显示的代理地址一致。")
    return 0


def cmd_info(args: argparse.Namespace, prompt: Prompt) -> int:
    _print_info(keystore.read_info(args.file))
    return 0


def cmd_check(args: argparse.Namespace, prompt: Prompt) -> int:
    _, info = keystore.load(args.file, prompt("口令: "))
    _say("口令正确，文件完整。")
    _print_info(info)
    return 0


def _print_info(info: keystore.KeyInfo) -> None:
    _say(f"  场所: {info.venue.value}")
    _say(f"  标签: {info.label}")
    _say(f"  地址: {info.address or '（该场所的密钥没有以太坊地址）'}")
    _say(f"  指纹: {info.fingerprint}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="humpback")
    sub = parser.add_subparsers(dest="group", required=True)
    ks = sub.add_parser("keystore", help="加密保存交易所代理密钥")
    ks_sub = ks.add_subparsers(dest="command", required=True)

    create = ks_sub.add_parser("create", help="新建加密密钥文件")
    create.add_argument(
        "--venue", required=True, choices=[v.value for v in Venue if v is not Venue.SIM]
    )
    create.add_argument("--label", required=True, help="例如 hl-testnet-agent-1")
    create.add_argument("--out", required=True, help="例如 var/keys/hl-testnet.keystore.json")
    create.set_defaults(func=cmd_create)

    info = ks_sub.add_parser("info", help="查看公开信息（不需要口令）")
    info.add_argument("file")
    info.set_defaults(func=cmd_info)

    check = ks_sub.add_parser("check", help="验证口令和文件完整性（不显示私钥）")
    check.add_argument("file")
    check.set_defaults(func=cmd_check)
    return parser


def main(argv: Sequence[str] | None = None, prompt: Prompt = getpass.getpass) -> int:
    args = build_parser().parse_args(argv)
    try:
        code: int = args.func(args, prompt)
    except keystore.KeystoreError as exc:
        _say(f"错误：{exc}")
        return 2
    return code


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
