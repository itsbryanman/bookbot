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
                            local exts='!*.@(mp3|m4a|m4b|aax|aaxc|flac|ogg|opus|aac|wav)'
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
                                    COMPREPLY+=( $(compgen -f -X '!*.@(aax|aaxc)' -- ${cur}) )
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
                        '--profile=[Configuration profile]:profile:(default plex audible series safe full)' \\
                        '--recurse=[Recursion depth]:depth:' \\
                        '--no-tag[Skip tagging operations]' \\
                        '--template=[Naming template]:template:(default plex audible series)' \\
                        '--lang=[Language preference]:lang:(en es fr de it pt ru zh ja)' \\
                        '--cache=[Cache directory]:directory:_directories' \\
                        '--log=[Log file]:file:_files' \\
                        '--help[Show help]' \\
                        '*:directory:_directories'
                    ;;
                tui)
                    _arguments \\
                        '--profile=[Configuration profile]:profile:(default plex audible series safe full)' \\
                        '--help[Show help]' \\
                        '*:directory:_directories'
                    ;;
                convert)
                    _arguments \\
                        '(-o --output)'{-o,--output}'=[Output directory]:directory:_directories' \\
                        '--profile=[Configuration profile]:profile:(default plex audible series)' \\
                        '--bitrate=[Audio bitrate]:bitrate:(64k 96k 128k 160k 192k 256k 320k)' \\
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
                                '*:file:_files -g "*.{mp3,m4a,m4b,aax,aaxc,flac,ogg,opus,aac,wav}"'
                            ;;
                        remove)
                            _arguments \\
                                '(-o --output-dir)'{-o,--output-dir}'=[Output directory]:directory:_directories' \\
                                '--activation-bytes=[Activation bytes]:bytes:' \\
                                '--dry-run[Show what would be done]' \\
                                '--recursive[Process recursively]' \\
                                '--help[Show help]' \\
                                '*:file:_files -g "*.{aax,aaxc}"'
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

# Main commands
complete -c bookbot -f -n '__fish_use_subcommand' -a 'scan' -d 'Scan a folder for audiobooks'
complete -c bookbot -f -n '__fish_use_subcommand' -a 'tui' -d 'Launch interactive TUI'
complete -c bookbot -f -n '__fish_use_subcommand' -a 'convert' -d 'Convert audiobooks to M4B'
complete -c bookbot -f -n '__fish_use_subcommand' -a 'undo' -d 'Undo a transaction'
complete -c bookbot -f -n '__fish_use_subcommand' -a 'config' -d 'Configuration management'
complete -c bookbot -f -n '__fish_use_subcommand' -a 'drm' -d 'DRM detection and removal'
complete -c bookbot -f -n '__fish_use_subcommand' -a 'provider' -d 'Provider management'
complete -c bookbot -f -n '__fish_use_subcommand' -a 'history' -d 'Show operation history'
complete -c bookbot -f -n '__fish_use_subcommand' -l help -d 'Show help'
complete -c bookbot -f -n '__fish_use_subcommand' -l version -d 'Show version'

# scan command
complete -c bookbot -f -n '__fish_seen_subcommand_from scan' -l dry-run -d 'Show what would be done'
complete -c bookbot -f -n '__fish_seen_subcommand_from scan' -l profile -a 'default plex audible series safe full' -d 'Configuration profile'
complete -c bookbot -f -n '__fish_seen_subcommand_from scan' -l recurse -d 'Recursion depth'
complete -c bookbot -f -n '__fish_seen_subcommand_from scan' -l no-tag -d 'Skip tagging operations'
complete -c bookbot -f -n '__fish_seen_subcommand_from scan' -l template -a 'default plex audible series' -d 'Naming template'
complete -c bookbot -f -n '__fish_seen_subcommand_from scan' -l lang -a 'en es fr de it pt ru zh ja' -d 'Language preference'
complete -c bookbot -f -n '__fish_seen_subcommand_from scan' -l cache -d 'Cache directory'
complete -c bookbot -f -n '__fish_seen_subcommand_from scan' -l log -d 'Log file'
complete -c bookbot -f -n '__fish_seen_subcommand_from scan' -l help -d 'Show help'

# tui command
complete -c bookbot -f -n '__fish_seen_subcommand_from tui' -l profile -a 'default plex audible series safe full' -d 'Configuration profile'
complete -c bookbot -f -n '__fish_seen_subcommand_from tui' -l help -d 'Show help'

# convert command
complete -c bookbot -f -n '__fish_seen_subcommand_from convert' -s o -l output -d 'Output directory'
complete -c bookbot -f -n '__fish_seen_subcommand_from convert' -l profile -a 'default plex audible series' -d 'Configuration profile'
complete -c bookbot -f -n '__fish_seen_subcommand_from convert' -l bitrate -a '64k 96k 128k 160k 192k 256k 320k' -d 'Audio bitrate'
complete -c bookbot -f -n '__fish_seen_subcommand_from convert' -l vbr -a '1 2 3 4 5 6' -d 'VBR quality'
complete -c bookbot -f -n '__fish_seen_subcommand_from convert' -l normalize -d 'Normalize audio levels'
complete -c bookbot -f -n '__fish_seen_subcommand_from convert' -l chapters -a 'auto from-tags' -d 'Chapter creation method'
complete -c bookbot -f -n '__fish_seen_subcommand_from convert' -l no-art -d 'Skip cover art'
complete -c bookbot -f -n '__fish_seen_subcommand_from convert' -l dry-run -d 'Show conversion plan'
complete -c bookbot -f -n '__fish_seen_subcommand_from convert' -l help -d 'Show help'

# config subcommands
complete -c bookbot -f -n '__fish_seen_subcommand_from config; and not __fish_seen_subcommand_from list show reset' -a 'list' -d 'List profiles'
complete -c bookbot -f -n '__fish_seen_subcommand_from config; and not __fish_seen_subcommand_from list show reset' -a 'show' -d 'Show configuration'
complete -c bookbot -f -n '__fish_seen_subcommand_from config; and not __fish_seen_subcommand_from list show reset' -a 'reset' -d 'Reset to defaults'
complete -c bookbot -f -n '__fish_seen_subcommand_from config; and __fish_seen_subcommand_from show' -a 'default plex audible series safe full' -d 'Profile name'

# drm subcommands
complete -c bookbot -f -n '__fish_seen_subcommand_from drm; and not __fish_seen_subcommand_from detect remove' -a 'detect' -d 'Detect DRM protection'
complete -c bookbot -f -n '__fish_seen_subcommand_from drm; and not __fish_seen_subcommand_from detect remove' -a 'remove' -d 'Remove DRM protection'
complete -c bookbot -f -n '__fish_seen_subcommand_from drm; and __fish_seen_subcommand_from detect' -l recursive -d 'Scan recursively'
complete -c bookbot -f -n '__fish_seen_subcommand_from drm; and __fish_seen_subcommand_from detect' -l help -d 'Show help'
complete -c bookbot -f -n '__fish_seen_subcommand_from drm; and __fish_seen_subcommand_from remove' -s o -l output-dir -d 'Output directory'
complete -c bookbot -f -n '__fish_seen_subcommand_from drm; and __fish_seen_subcommand_from remove' -l activation-bytes -d 'Activation bytes'
complete -c bookbot -f -n '__fish_seen_subcommand_from drm; and __fish_seen_subcommand_from remove' -l dry-run -d 'Show what would be done'
complete -c bookbot -f -n '__fish_seen_subcommand_from drm; and __fish_seen_subcommand_from remove' -l recursive -d 'Process recursively'
complete -c bookbot -f -n '__fish_seen_subcommand_from drm; and __fish_seen_subcommand_from remove' -l help -d 'Show help'

# provider subcommands
complete -c bookbot -f -n '__fish_seen_subcommand_from provider; and not __fish_seen_subcommand_from list enable disable set-key set-marketplace' -a 'list' -d 'List providers'
complete -c bookbot -f -n '__fish_seen_subcommand_from provider; and not __fish_seen_subcommand_from list enable disable set-key set-marketplace' -a 'enable' -d 'Enable provider'
complete -c bookbot -f -n '__fish_seen_subcommand_from provider; and not __fish_seen_subcommand_from list enable disable set-key set-marketplace' -a 'disable' -d 'Disable provider'
complete -c bookbot -f -n '__fish_seen_subcommand_from provider; and not __fish_seen_subcommand_from list enable disable set-key set-marketplace' -a 'set-key' -d 'Set API key'
complete -c bookbot -f -n '__fish_seen_subcommand_from provider; and not __fish_seen_subcommand_from list enable disable set-key set-marketplace' -a 'set-marketplace' -d 'Set marketplace'
complete -c bookbot -f -n '__fish_seen_subcommand_from provider; and __fish_seen_subcommand_from enable' -a 'googlebooks librivox audible' -d 'Provider name'
complete -c bookbot -f -n '__fish_seen_subcommand_from provider; and __fish_seen_subcommand_from disable' -a 'googlebooks librivox audible' -d 'Provider name'
complete -c bookbot -f -n '__fish_seen_subcommand_from provider; and __fish_seen_subcommand_from set-key' -a 'googlebooks' -d 'Provider name'
complete -c bookbot -f -n '__fish_seen_subcommand_from provider; and __fish_seen_subcommand_from set-marketplace' -a 'US UK CA AU FR DE IT ES JP IN' -d 'Marketplace'

# history command
complete -c bookbot -f -n '__fish_seen_subcommand_from history' -l days -d 'Number of days'
complete -c bookbot -f -n '__fish_seen_subcommand_from history' -l help -d 'Show help'

# undo command
complete -c bookbot -f -n '__fish_seen_subcommand_from undo' -l help -d 'Show help'
"""


@click.command()
@click.argument('shell', type=click.Choice(['bash', 'zsh', 'fish', 'all']))
@click.option('--output-dir', '-o', type=click.Path(), help='Output directory for completion files')
def main(shell: str, output_dir: str) -> None:
    """Generate shell completion scripts for BookBot."""
    import os
    from pathlib import Path

    output_path = Path(output_dir) if output_dir else Path.cwd() / "completions"
    output_path.mkdir(exist_ok=True)

    generators = {
        'bash': ('bookbot.bash', generate_bash_completion),
        'zsh': ('_bookbot', generate_zsh_completion),
        'fish': ('bookbot.fish', generate_fish_completion)
    }

    if shell == 'all':
        shells_to_generate = ['bash', 'zsh', 'fish']
    else:
        shells_to_generate = [shell]

    for shell_name in shells_to_generate:
        filename, generator = generators[shell_name]
        content = generator()

        file_path = output_path / filename
        with open(file_path, 'w') as f:
            f.write(content)

        # Make bash script executable
        if shell_name == 'bash':
            os.chmod(file_path, 0o755)

        click.echo(f"Generated {shell_name} completion: {file_path}")

    click.echo("\nTo install completions:")

    if 'bash' in shells_to_generate:
        click.echo(f"  Bash: source {output_path / 'bookbot.bash'}")
        click.echo("        or copy to /etc/bash_completion.d/")

    if 'zsh' in shells_to_generate:
        click.echo(f"  Zsh:  add {output_path} to your fpath")
        click.echo("        or copy _bookbot to any directory in $fpath")

    if 'fish' in shells_to_generate:
        click.echo(f"  Fish: copy {output_path / 'bookbot.fish'} to ~/.config/fish/completions/")


if __name__ == '__main__':
    main()
