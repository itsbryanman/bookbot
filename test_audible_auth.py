#!/usr/bin/env python3
"""Test script for Audible browser-based authentication."""

from bookbot.drm.audible_client import AudibleAuthClient


def main():
    """Test Audible authentication flow."""
    print("=" * 60)
    print("Audible Browser-Based Authentication Test")
    print("=" * 60)
    print()

    # Initialize client
    client = AudibleAuthClient(country_code="US")

    # Check if already authenticated
    if client.is_authenticated():
        print("✅ Already authenticated!")
        print()
        print("Testing library access...")
        try:
            library = client.get_library()
            print(f"✅ Successfully retrieved library with {len(library)} items")
            if library:
                print("\nFirst few items:")
                for book in library[:3]:
                    print(f"  - {book.get('title')} by {book.get('authors', [{}])[0].get('name', 'Unknown')}")
        except Exception as e:
            print(f"❌ Failed to get library: {e}")
    else:
        print("No existing authentication found.")
        print()
        print("Starting authentication process...")
        print("This will open a browser window where you can log in.")
        print()

        # Authenticate
        success = client.authenticate(headless=False)

        if success:
            print()
            print("=" * 60)
            print("✅ Authentication successful!")
            print("=" * 60)
            print()
            print("Testing library access...")
            try:
                library = client.get_library()
                print(f"✅ Successfully retrieved library with {len(library)} items")
                if library:
                    print("\nFirst few items:")
                    for book in library[:5]:
                        print(f"  - {book.get('title')} by {book.get('authors', [{}])[0].get('name', 'Unknown')}")
            except Exception as e:
                print(f"❌ Failed to get library: {e}")
        else:
            print()
            print("=" * 60)
            print("❌ Authentication failed")
            print("=" * 60)
            return 1

    print()
    print("=" * 60)
    print("Test completed!")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    exit(main())
