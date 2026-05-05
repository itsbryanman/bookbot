#!/usr/bin/env python3
"""Generate shell completion scripts for BookBot."""

import click


def generate_bash_completion() -> str:
    """Generate bash completion script."""
    return """#!/bin/bash

_bookbot_completion() {
    local cur prev opts
    COMPREPLY=()
    cur="${COMP_WORDS[COMP_CWORD]}"
    prev="${COMP_WORDS[COMP_CWORD-1]}"

    # Main commands
    if [[ ${COMP_CWORD} == 1 ]]; then
        opts="scan tui convert undo config drm provider history --help --version"
        COMPREPLY=( $(compgen -W "${opts}" -- ${cur}) )
        return 0
    fi

    # Command-specific completions
    case "${COMP_WORDS[1]}" in
        scan)
            case "${prev}" in
                --profile)
                    COMPREPLY=( $(compgen -W "default plex audible series safe full" \
                        -- ${cur}) )
                    return 0
                    ;;
                --template)
                    COMPREPLY=( $(compgen -W "default plex audible series" -- ${cur}) )
                    return 0
                    ;;
                --lang)
                    COMPREPLY=( $(compgen -W "en es fr de it pt ru zh ja" -- ${cur}) )
                    return 0
                    ;;
                *)
                    opts="--dry-run --profile --recurse --no-tag --template --lang"
                    opts+=" --cache --log --help"
                    COMPREPLY=( $(compgen -W "${opts}" -- ${cur}) )
                    if [[ ${cur} != -* ]]; then
                        COMPREPLY+=( $(compgen -d -- ${cur}) )
                    fi
                    return 0
                    ;;
            esac
            ;;
        tui)
            case "${prev}" in
                --profile)
                    COMPREPLY=( $(compgen -W "default plex audible series safe full" \
                        -- ${cur}) )
                    return 0
                    ;;
                *)
                    opts="--profile --help"
                    COMPREPLY=( $(compgen -W "${opts}" -- ${cur}) )
                    if [[ ${cur} != -* ]]; then
                        COMPREPLY+=( $(compgen -d -- ${cur}) )
                    fi
                    return 0
                    ;;
            esac
            ;;
        convert)
            case "${prev}" in
                -o|--output)
                    COMPREPLY=( $(compgen -d -- ${cur}) )
                    return 0
                    ;;
                --profile)
                    COMPREPLY=( $(compgen -W "default plex audible series" -- ${cur}) )
                    return 0
                    ;;
                --bitrate)
                    COMPREPLY=( $(compgen -W "64k 96k 128k 160k 192k 256k 320k" \
                        -- ${cur}) )
                    return 0
                    ;;
                --vbr)
                    COMPREPLY=( $(compgen -W "1 2 3 4 5 6" -- ${cur}) )
                    return 0
                    ;;
                --chapters)
                    COMPREPLY=( $(compgen -W "auto from-tags" -- ${cur}) )
                    return 0
                    ;;
                *)
                    opts="-o --output --profile --bitrate --vbr --normalize --chapters"
                    opts+=" --no-art --dry-run --help"
                    COMPREPLY=( $(compgen -W "${opts}" -- ${cur}) )
                    if [[ ${cur} != -* ]]; then
                        COMPREPLY+=( $(compgen -d -- ${cur}) )
                    fi
                    return 0
                    ;;
            esac
            ;;
        config)
            if [[ ${COMP_CWORD} == 2 ]]; then
                opts="list show reset"
                COMPREPLY=( $(compgen -W "${opts}" -- ${cur}) )
                return 0
            elif [[ ${COMP_CWORD} == 3 && "${COMP_WORDS[2]}" == "show" ]]; then
                opts="default plex audible series safe full"
                COMPREPLY=( $(compgen -W "${opts}" -- ${cur}) )
                return 0
            fi
            ;;
        drm)
            if [[ ${COMP_CWORD} == 2 ]]; then
                opts="detect remove"
                COMPREPLY=( $(compgen -W "${opts}" -- ${cur}) )
                return 0
            else
                case "${COMP_WORDS[2]}" in
                    detect)
                        opts="--recursive --help"
                        COMPREPLY=( $(compgen -W "${opts}" -- ${cur}) )
                        if [[ ${cur} != -* ]]; then
                            local exts='!*.@(mp3|m4a|m4b|aax|aaxc|'
                            exts+='flac|ogg|opus|aac|wav)'
                            COMPREPLY+=( $(compgen -f -X "${exts}" -- ${cur}) )
                            COMPREPLY+=( $(compgen -d -- ${cur}) )
                        fi
                        return 0
                        ;;
                    remove)
                        case "${prev}" in
                            -o|--output-dir)
                                COMPREPLY=( $(compgen -d -- ${cur}) )
                                return 0
                                ;;
                            *)
                                opts="-o --output-dir --activation-bytes --dry-run"
                                opts+=" --recursive --help"
                                COMPREPLY=( $(compgen -W "${opts}" -- ${cur}) )
                                if [[ ${cur} != -* ]]; then
                                    local drm_exts='!*.@(aax|aaxc)'
                                    COMPREPLY+=( \
                                        $(compgen -f -X "${drm_exts}" -- ${cur}) \
                                    )
                                    COMPREPLY+=( $(compgen -d -- ${cur}) )
                                fi
                                return 0
                                ;;
                        esac
                        ;;
                esac
            fi
            ;;
        provider)
            if [[ ${COMP_CWORD} == 2 ]]; then
                opts="list enable disable set-key set-marketplace"
                COMPREPLY=( $(compgen -W "${opts}" -- ${cur}) )
                return 0
            elif [[ ${COMP_CWORD} == 3 ]]; then
                case "${COMP_WORDS[2]}" in
                    enable|disable)
                        opts="googlebooks librivox audible"
                        COMPREPLY=( $(compgen -W "${opts}" -- ${cur}) )
                        return 0
                        ;;
                    set-key)
                        opts="googlebooks"
                        COMPREPLY=( $(compgen -W "${opts}" -- ${cur}) )
                        return 0
                        ;;
                    set-marketplace)
                        opts="US UK CA AU FR DE IT ES JP IN"
                        COMPREPLY=( $(compgen -W "${opts}" -- ${cur}) )
                        return 0
                        ;;
                esac
            fi
            ;;
        history)
            opts="--days --help"
            COMPREPLY=( $(compgen -W "${opts}" -- ${cur}) )
            return 0
            ;;
    esac
}

complete -F _bookbot_completion bookbot
"""


def generate_zsh_completion() -> str:
    """Generate zsh completion script."""
    return """#compdef bookbot

_bookbot() {
    local context state line
    typeset -A opt_args
    local profiles='(default plex audible series safe full)'
    local templates='(default plex audible series)'
    local scan_languages='(en es fr de it pt ru zh ja)'
    local bitrates='(64k 96k 128k 160k 192k 256k 320k)'
    local audio_glob='*.{mp3,m4a,m4b,aax,aaxc,flac,ogg,opus,aac,wav}'
    local drm_glob='*.{aax,aaxc}'
    local out_spec='(-o --output){-o,--output}=[Output directory]:'
    out_spec+='directory:_directories'
    local drm_out_spec='(-o --output-dir){-o,--output-dir}='
    drm_out_spec+='[Output directory]:directory:_directories'

    _arguments -C \\
        '1: :->command' \\
        '*: :->args'

    case $state in
        command)
            _values 'bookbot command' \\
                'scan[Scan a folder for audiobooks]' \\
                'tui[Launch interactive TUI]' \\
                'convert[Convert audiobooks to M4B]' \\
                'undo[Undo a transaction]' \\
                'config[Configuration management]' \\
                'drm[DRM detection and removal]' \\
                'provider[Provider management]' \\
                'history[Show operation history]' \\
                '--help[Show help]' \\
                '--version[Show version]'
            ;;
        args)
            case $words[2] in
                scan)
                    _arguments \\
                        '--dry-run[Show what would be done]' \\
                        '--profile=[Configuration profile]:profile:'"${profiles}" \\
                        '--recurse=[Recursion depth]:depth:' \\
                        '--no-tag[Skip tagging operations]' \\
                        '--template=[Naming template]:template:'"${templates}" \\
                        '--lang=[Language preference]:lang:'"${scan_languages}" \\
                        '--cache=[Cache directory]:directory:_directories' \\
                        '--log=[Log file]:file:_files' \\
                        '--help[Show help]' \\
                        '*:directory:_directories'
                    ;;
                tui)
                    _arguments \\
                        '--profile=[Configuration profile]:profile:'"${profiles}" \\
                        '--help[Show help]' \\
                        '*:directory:_directories'
                    ;;
                convert)
                    _arguments \\
                        "${out_spec}" \\
                        '--profile=[Configuration profile]:profile:'"${templates}" \\
                        '--bitrate=[Audio bitrate]:bitrate:'"${bitrates}" \\
                        '--vbr=[VBR quality]:quality:(1 2 3 4 5 6)' \\
                        '--normalize[Normalize audio levels]' \\
                        '--chapters=[Chapter creation]:method:(auto from-tags)' \\
                        '--no-art[Skip cover art]' \\
                        '--dry-run[Show conversion plan]' \\
                        '--help[Show help]' \\
                        '*:directory:_directories'
                    ;;
                config)
                    case $words[3] in
                        show)
                            _arguments \\
                                ':profile:(default plex audible series safe full)'
                            ;;
                        *)
                            _values 'config command' \\
                                'list[List profiles]' \\
                                'show[Show configuration]' \\
                                'reset[Reset to defaults]'
                            ;;
                    esac
                    ;;
                drm)
                    case $words[3] in
                        detect)
                            _arguments \\
                                '--recursive[Scan recursively]' \\
                                '--help[Show help]' \\
                                '*:file:_files -g '"${audio_glob}"
                            ;;
                        remove)
                            _arguments \\
                                "${drm_out_spec}" \\
                                '--activation-bytes=[Activation bytes]:bytes:' \\
                                '--dry-run[Show what would be done]' \\
                                '--recursive[Process recursively]' \\
                                '--help[Show help]' \\
                                '*:file:_files -g '"${drm_glob}"
                            ;;
                        *)
                            _values 'drm command' \\
                                'detect[Detect DRM protection]' \\
                                'remove[Remove DRM protection]'
                            ;;
                    esac
                    ;;
                provider)
                    case $words[3] in
                        enable|disable)
                            _arguments \\
                                ':provider:(googlebooks librivox audible)'
                            ;;
                        set-key)
                            _arguments \\
                                ':provider:(googlebooks)' \\
                                ':api-key:'
                            ;;
                        set-marketplace)
                            _arguments \\
                                ':marketplace:(US UK CA AU FR DE IT ES JP IN)'
                            ;;
                        *)
                            _values 'provider command' \\
                                'list[List providers]' \\
                                'enable[Enable provider]' \\
                                'disable[Disable provider]' \\
                                'set-key[Set API key]' \\
                                'set-marketplace[Set marketplace]'
                            ;;
                    esac
                    ;;
                history)
                    _arguments \\
                        '--days=[Number of days]:days:' \\
                        '--help[Show help]'
                    ;;
                undo)
                    _arguments \\
                        ':transaction-id:' \\
                        '--help[Show help]'
                    ;;
            esac
            ;;
    esac
}

_bookbot "$@"
"""


def generate_fish_completion() -> str:
    """Generate fish completion script."""
    return """# BookBot completions for fish shell

set -l use_sub '__fish_use_subcommand'
set -l scan_cond '__fish_seen_subcommand_from scan'
set -l tui_cond '__fish_seen_subcommand_from tui'
set -l convert_cond '__fish_seen_subcommand_from convert'
set -l config_root '__fish_seen_subcommand_from config'
set -l config_gate "$config_root; and not __fish_seen_subcommand_from list show reset"
set -l config_show "$config_root; and __fish_seen_subcommand_from show"
set -l drm_root '__fish_seen_subcommand_from drm'
set -l drm_gate "$drm_root; and not __fish_seen_subcommand_from detect remove"
set -l drm_detect "$drm_root; and __fish_seen_subcommand_from detect"
set -l drm_remove "$drm_root; and __fish_seen_subcommand_from remove"
set -l provider_root '__fish_seen_subcommand_from provider'
set -l provider_subs 'list enable disable set-key set-marketplace'
set -l provider_gate \
    "$provider_root; and not __fish_seen_subcommand_from $provider_subs"
set -l provider_enable "$provider_root; and __fish_seen_subcommand_from enable"
set -l provider_disable "$provider_root; and __fish_seen_subcommand_from disable"
set -l provider_key "$provider_root; and __fish_seen_subcommand_from set-key"
set -l provider_market "$provider_root; and __fish_seen_subcommand_from set-marketplace"
set -l history_cond '__fish_seen_subcommand_from history'
set -l undo_cond '__fish_seen_subcommand_from undo'
set -l profiles 'default plex audible series safe full'
set -l templates 'default plex audible series'
set -l languages 'en es fr de it pt ru zh ja'
set -l bitrates '64k 96k 128k 160k 192k 256k 320k'
set -l providers 'googlebooks librivox audible'
set -l marketplaces 'US UK CA AU FR DE IT ES JP IN'

# Main commands
complete -c bookbot -f -n "$use_sub" \
    -a 'scan' \
    -d 'Scan a folder for audiobooks'
complete -c bookbot -f -n "$use_sub" \
    -a 'tui' \
    -d 'Launch interactive TUI'
complete -c bookbot -f -n "$use_sub" \
    -a 'convert' \
    -d 'Convert audiobooks to M4B'
complete -c bookbot -f -n "$use_sub" \
    -a 'undo' \
    -d 'Undo a transaction'
complete -c bookbot -f -n "$use_sub" \
    -a 'config' \
    -d 'Configuration management'
complete -c bookbot -f -n "$use_sub" \
    -a 'drm' \
    -d 'DRM detection and removal'
complete -c bookbot -f -n "$use_sub" \
    -a 'provider' \
    -d 'Provider management'
complete -c bookbot -f -n "$use_sub" \
    -a 'history' \
    -d 'Show operation history'
complete -c bookbot -f -n "$use_sub" -l help -d 'Show help'
complete -c bookbot -f -n "$use_sub" -l version -d 'Show version'

# scan command
complete -c bookbot -f -n "$scan_cond" -l dry-run \
    -d 'Show what would be done'
complete -c bookbot -f -n "$scan_cond" -l profile -a "$profiles" \
    -d 'Configuration profile'
complete -c bookbot -f -n "$scan_cond" -l recurse \
    -d 'Recursion depth'
complete -c bookbot -f -n "$scan_cond" -l no-tag \
    -d 'Skip tagging operations'
complete -c bookbot -f -n "$scan_cond" -l template -a "$templates" \
    -d 'Naming template'
complete -c bookbot -f -n "$scan_cond" -l lang -a "$languages" \
    -d 'Language preference'
complete -c bookbot -f -n "$scan_cond" -l cache -d 'Cache directory'
complete -c bookbot -f -n "$scan_cond" -l log -d 'Log file'
complete -c bookbot -f -n "$scan_cond" -l help -d 'Show help'

# tui command
complete -c bookbot -f -n "$tui_cond" -l profile -a "$profiles" \
    -d 'Configuration profile'
complete -c bookbot -f -n "$tui_cond" -l help -d 'Show help'

# convert command
complete -c bookbot -f -n "$convert_cond" -s o -l output \
    -d 'Output directory'
complete -c bookbot -f -n "$convert_cond" -l profile -a "$templates" \
    -d 'Configuration profile'
complete -c bookbot -f -n "$convert_cond" -l bitrate -a "$bitrates" \
    -d 'Audio bitrate'
complete -c bookbot -f -n "$convert_cond" -l vbr -a '1 2 3 4 5 6' \
    -d 'VBR quality'
complete -c bookbot -f -n "$convert_cond" -l normalize \
    -d 'Normalize audio levels'
complete -c bookbot -f -n "$convert_cond" -l chapters -a 'auto from-tags' \
    -d 'Chapter creation method'
complete -c bookbot -f -n "$convert_cond" -l no-art -d 'Skip cover art'
complete -c bookbot -f -n "$convert_cond" -l dry-run \
    -d 'Show conversion plan'
complete -c bookbot -f -n "$convert_cond" -l help -d 'Show help'

# config subcommands
complete -c bookbot -f -n "$config_gate" -a 'list' -d 'List profiles'
complete -c bookbot -f -n "$config_gate" -a 'show' -d 'Show configuration'
complete -c bookbot -f -n "$config_gate" -a 'reset' -d 'Reset to defaults'
complete -c bookbot -f -n "$config_show" -a "$profiles" \
    -d 'Profile name'

# drm subcommands
complete -c bookbot -f -n "$drm_gate" -a 'detect' \
    -d 'Detect DRM protection'
complete -c bookbot -f -n "$drm_gate" -a 'remove' \
    -d 'Remove DRM protection'
complete -c bookbot -f -n "$drm_detect" -l recursive \
    -d 'Scan recursively'
complete -c bookbot -f -n "$drm_detect" -l help -d 'Show help'
complete -c bookbot -f -n "$drm_remove" -s o -l output-dir \
    -d 'Output directory'
complete -c bookbot -f -n "$drm_remove" -l activation-bytes \
    -d 'Activation bytes'
complete -c bookbot -f -n "$drm_remove" -l dry-run \
    -d 'Show what would be done'
complete -c bookbot -f -n "$drm_remove" -l recursive \
    -d 'Process recursively'
complete -c bookbot -f -n "$drm_remove" -l help -d 'Show help'

# provider subcommands
complete -c bookbot -f -n "$provider_gate" -a 'list' \
    -d 'List providers'
complete -c bookbot -f -n "$provider_gate" -a 'enable' \
    -d 'Enable provider'
complete -c bookbot -f -n "$provider_gate" -a 'disable' \
    -d 'Disable provider'
complete -c bookbot -f -n "$provider_gate" -a 'set-key' \
    -d 'Set API key'
complete -c bookbot -f -n "$provider_gate" -a 'set-marketplace' \
    -d 'Set marketplace'
complete -c bookbot -f -n "$provider_enable" -a "$providers" \
    -d 'Provider name'
complete -c bookbot -f -n "$provider_disable" -a "$providers" \
    -d 'Provider name'
complete -c bookbot -f -n "$provider_key" -a 'googlebooks' \
    -d 'Provider name'
complete -c bookbot -f -n "$provider_market" -a "$marketplaces" \
    -d 'Marketplace'

# history command
complete -c bookbot -f -n "$history_cond" -l days -d 'Number of days'
complete -c bookbot -f -n "$history_cond" -l help -d 'Show help'

# undo command
complete -c bookbot -f -n "$undo_cond" -l help -d 'Show help'
"""


@click.command()
@click.argument("shell", type=click.Choice(["bash", "zsh", "fish", "all"]))
@click.option(
    "--output-dir",
    "-o",
    type=click.Path(),
    help="Output directory for completion files",
)
def main(shell: str, output_dir: str) -> None:
    """Generate shell completion scripts for BookBot."""
    import os
    from pathlib import Path

    output_path = Path(output_dir) if output_dir else Path.cwd() / "completions"
    output_path.mkdir(exist_ok=True)

    generators = {
        "bash": ("bookbot.bash", generate_bash_completion),
        "zsh": ("_bookbot", generate_zsh_completion),
        "fish": ("bookbot.fish", generate_fish_completion),
    }

    if shell == "all":
        shells_to_generate = ["bash", "zsh", "fish"]
    else:
        shells_to_generate = [shell]

    for shell_name in shells_to_generate:
        filename, generator = generators[shell_name]
        content = generator()

        file_path = output_path / filename
        with open(file_path, "w") as f:
            f.write(content)

        # Make bash script executable
        if shell_name == "bash":
            os.chmod(file_path, 0o755)

        click.echo(f"Generated {shell_name} completion: {file_path}")

    click.echo("\nTo install completions:")

    if "bash" in shells_to_generate:
        click.echo(f"  Bash: source {output_path / 'bookbot.bash'}")
        click.echo("        or copy to /etc/bash_completion.d/")

    if "zsh" in shells_to_generate:
        click.echo(f"  Zsh:  add {output_path} to your fpath")
        click.echo("        or copy _bookbot to any directory in $fpath")

    if "fish" in shells_to_generate:
        click.echo(
            f"  Fish: copy {output_path / 'bookbot.fish'} "
            "to ~/.config/fish/completions/"
        )


if __name__ == "__main__":
    main()
