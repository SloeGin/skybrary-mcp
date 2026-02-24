import fs from 'fs/promises';
import path from 'path';
import { fileURLToPath } from 'url';
import crypto from 'crypto';
import { existsSync } from 'fs';

interface CacheEntry {
  timestamp: number;
  content: string;
}

export class CacheManager {
  private cacheDir: string;
  private ttlSeconds: number;
  private sessionStartTime: number;
  private canaryChecked: boolean = false;
  private cacheInvalidated: boolean = false;

  constructor(cacheDir: string = "../data/cache", ttlDays: number = 30) {
    // Resolve path relative to this file (src/cacheManager.ts -> ../data/cache)
    const __filename = fileURLToPath(import.meta.url);
    const __dirname = path.dirname(__filename);
    this.cacheDir = path.resolve(__dirname, '..', cacheDir);
    this.ttlSeconds = ttlDays * 24 * 3600;
    this.sessionStartTime = Date.now() / 1000;
    
    // Ensure cache directory exists
    fs.mkdir(this.cacheDir, { recursive: true }).catch(err => {
        console.error(`Failed to create cache dir: ${err}`);
    });
  }

  private getFilePath(key: string): string {
    const hashedKey = crypto.createHash('md5').update(key).digest('hex');
    return path.join(this.cacheDir, `${hashedKey}.json`);
  }

  async getContent(key: string, fetchFunc: () => Promise<string>): Promise<string> {
    const filePath = this.getFilePath(key);
    const fileExists = existsSync(filePath);

    // 1. If not in cache, simple fetch and save
    if (!fileExists) {
      const freshContent = await fetchFunc();
      await this.saveCache(filePath, freshContent);
      return freshContent;
    }

    // Load cached data
    let cachedData: CacheEntry;
    try {
      const raw = await fs.readFile(filePath, 'utf-8');
      cachedData = JSON.parse(raw);
    } catch (e) {
      // Corrupt cache, treat as missing
      const freshContent = await fetchFunc();
      await this.saveCache(filePath, freshContent);
      return freshContent;
    }

    const cachedContent = cachedData.content;
    const cachedTs = cachedData.timestamp;

    // 2. Canary Check Logic (First hit on a cached item)
    if (!this.canaryChecked) {
      try {
        const freshContent = await fetchFunc();
        this.canaryChecked = true;

        if (freshContent !== cachedContent) {
            console.error(`[Cache] Canary mismatch for '${key}'. Invalidating all old caches.`);
            this.cacheInvalidated = true;
            await this.saveCache(filePath, freshContent);
            return freshContent;
        } else {
            console.error(`[Cache] Canary match for '${key}'. Trusting existing caches.`);
            this.cacheInvalidated = false;
            // Update timestamp to refresh TTL for this item
            await this.saveCache(filePath, freshContent);
            return freshContent;
        }
      } catch (e) {
        console.error(`[Cache] Canary fetch failed for '${key}': ${e}. Trusting cache for now.`);
        return cachedContent;
      }
    }

    // 3. Subsequent Cache Hits
    let shouldRefresh = false;
    const now = Date.now() / 1000;
    
    if (this.cacheInvalidated) {
        // If invalidated, any file older than this session is considered expired
        if (cachedTs < this.sessionStartTime) {
            shouldRefresh = true;
        }
    } else if (now - cachedTs >= this.ttlSeconds) {
        shouldRefresh = true;
    }

    if (shouldRefresh) {
        try {
            const freshContent = await fetchFunc();
            await this.saveCache(filePath, freshContent);
            return freshContent;
        } catch (e) {
            console.error(`[Cache] Refresh failed for '${key}': ${e}. Returning stale cache.`);
            return cachedContent;
        }
    }

    return cachedContent;
  }

  private async saveCache(filePath: string, content: string) {
    const entry: CacheEntry = {
        timestamp: Date.now() / 1000,
        content
    };
    await fs.writeFile(filePath, JSON.stringify(entry, null, 2), 'utf-8');
  }
}
