# OpenSea API Update

## Changes Made

The Neverland marketplace proxy API (`https://app.neverland.money/api/marketplace/opensea`) was returning 404 errors and has been replaced with direct OpenSea API integration.

### Files Updated

1. **backend/app.py**
   - Changed from Neverland proxy URL to OpenSea API v2 endpoint
   - Added OpenSea API key authentication
   - Updated `_fetch_json()` to support custom headers
   - Updated `fetch_opensea_listings()` to use new endpoint format

2. **backend/monitor.py**
   - Changed from Neverland proxy URL to OpenSea API v2 endpoint
   - Added OpenSea API key authentication
   - Updated `request_json_with_retry()` to accept URL parameter and headers
   - Updated `fetch_all_opensea_listings()` to use new endpoint format

### API Configuration

- **New Endpoint**: `https://api.opensea.io/api/v2/listings/collection/{slug}/all`
- **Authentication**: Uses `X-API-KEY` header
- **API Key**: Set via `OPENSEA_API_KEY` environment variable (defaults to hardcoded key)

### Environment Variable (Optional)

You can set the API key via environment variable instead of using the hardcoded default:

```bash
export OPENSEA_API_KEY="your-api-key-here"
```

### Testing

Both files have been tested and confirmed working:
- Successfully fetches listings from OpenSea API
- Properly handles pagination with `next` cursor
- Maintains same data structure as before

### Collection Details

- **Collection**: voting-escrow-dust
- **Blockchain**: Monad
- **Contract**: 0x3bd359c1119da7da1d913d1c4d2b7c461115433a
- **OpenSea URL**: https://opensea.io/collection/voting-escrow-dust

### Security Note

⚠️ **Important**: The API key is currently hardcoded in the files. For production use, you should:
1. Remove the hardcoded API key
2. Set `OPENSEA_API_KEY` environment variable
3. Never commit API keys to version control
