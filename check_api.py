"""Verify the Anthropic API key works, without spending anything.

Uses the token-counting endpoint, which authenticates the key but generates no
tokens and so is not billed. Run this before the first real run so an auth
problem surfaces here rather than partway through a paid job.
"""

import sys

import anthropic

from ai_monitor.analysis.analyzer import ANALYZER_MODEL
from ai_monitor.config.settings import settings
from ai_monitor.eval.judge import JUDGE_MODEL
from ai_monitor.synthesis.synthesizer import SYNTHESIS_MODEL


def main() -> int:
    key = settings.anthropic_api_key

    if not key:
        print("✗ ANTHROPIC_API_KEY is not set.")
        print()
        print("  Add it to .env in the project root:")
        print("    ANTHROPIC_API_KEY=sk-ant-api03-...")
        print()
        print("  See docs/api-setup.md for how to get one.")
        return 1

    if not key.startswith("sk-ant-"):
        print(f"✗ That does not look like an Anthropic API key (starts {key[:8]!r}).")
        print("  Console API keys begin with 'sk-ant-api'. A claude.ai session")
        print("  credential will not work here - see docs/api-setup.md.")
        return 1

    print(f"key: {key[:14]}...{key[-4:]}")
    client = anthropic.Anthropic(api_key=key)

    try:
        # Counting tokens authenticates without generating any.
        result = client.messages.count_tokens(
            model=ANALYZER_MODEL,
            messages=[{"role": "user", "content": "ping"}],
        )
    except anthropic.AuthenticationError:
        print("✗ Authentication failed - the key was rejected.")
        print("  It may be revoked, mistyped, or from the wrong product.")
        return 1
    except anthropic.PermissionDeniedError:
        print("✗ Key is valid but lacks permission.")
        print("  Check the workspace has credit at console.anthropic.com/settings/billing")
        return 1
    except anthropic.APIStatusError as exc:
        print(f"✗ API error {exc.status_code}: {exc.message}")
        if "credit" in str(exc.message).lower():
            print("  Add credits in the Console before running the pipeline.")
        return 1
    except anthropic.APIConnectionError:
        print("✗ Could not reach the API. Check your network.")
        return 1

    print(f"✓ Key works (counted {result.input_tokens} tokens, billed nothing)")
    print()
    print("models this project will use:")
    print(f"  analysis   {ANALYZER_MODEL}")
    print(f"  synthesis  {SYNTHESIS_MODEL}")
    print(f"  eval judge {JUDGE_MODEL}")
    print()
    print("next:")
    print("  python run.py --provider anthropic --skip-fetch   # re-analyze on Haiku")
    print("  python evaluate.py                                # judge + comparisons")
    return 0


if __name__ == "__main__":
    sys.exit(main())
