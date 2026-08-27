"""
Модуль автоматической верификации научных статей Scopus / Web of Science
Использует открытые международные академические API: OpenAlex и CrossRef.
Работает асинхронно, с таймаутом, не блокируя работу бота.
"""

import aiohttp
import asyncio
import urllib.parse
import re
import difflib
import logging

logger = logging.getLogger(__name__)

# Регулярка для поиска DOI в тексте
DOI_REGEX = re.compile(r'\b(10\.\d{4,9}/[-._;()/:A-Za-z0-9]+)\b')


def extract_doi_from_text(text: str) -> str:
    """Извлекает DOI из текста или ссылки, если пользователь его вставил"""
    if not text:
        return ""
    match = DOI_REGEX.search(text)
    return match.group(1).rstrip('.') if match else ""


def calculate_similarity(a: str, b: str) -> float:
    """Вычисляет сходство двух названий от 0.0 до 1.0"""
    if not a or not b:
        return 0.0
    clean_a = re.sub(r'[^\w\s]', '', a.lower()).strip()
    clean_b = re.sub(r'[^\w\s]', '', b.lower()).strip()
    return difflib.SequenceMatcher(None, clean_a, clean_b).ratio()


async def verify_article(title: str = "", journal: str = "", authors: str = "", url_or_doi: str = "") -> dict:
    """
    Автоматическая верификация научной статьи через открытые академические базы данных.
    
    Возвращает словарь:
    {
        "status": "found" | "not_found" | "error",
        "is_scopus": bool,
        "matched_title": str,
        "journal": str,
        "year": str,
        "doi": str,
        "badge": str,
        "badge_excel": str
    }
    """
    if not title and not url_or_doi:
        return {
            "status": "not_found",
            "is_scopus": False,
            "badge": "⚠️ Маълумот етарли эмас",
            "badge_excel": "⚠️ Маълумот йўқ"
        }

    # 1. Проверяем, есть ли DOI прямо в тексте названия, URL или поля
    found_doi = extract_doi_from_text(url_or_doi) or extract_doi_from_text(title)
    
    headers = {
        "User-Agent": "ADTI-Scientific-Verifier/1.0 (mailto:science@adti.uz)"
    }
    timeout = aiohttp.ClientTimeout(total=6)

    try:
        async with aiohttp.ClientSession(headers=headers, timeout=timeout) as session:
            # ── СПОСОБ А: Если найден DOI — делаем прямой точный запрос ──
            if found_doi:
                openalex_doi_url = f"https://api.openalex.org/works/doi:{found_doi}"
                try:
                    async with session.get(openalex_doi_url) as resp:
                        if resp.status == 200:
                            w = await resp.json()
                            prim = w.get("primary_location") or {}
                            source = prim.get("source") or {}
                            j_name = source.get("display_name") or journal or "Илмий журнал"
                            is_scopus = source.get("is_in_scopus", True)
                            year = str(w.get("publication_year") or "")
                            
                            badge = f"✅ Халқаро база: {j_name} ({year}) | DOI: {found_doi}"
                            badge_excel = f"✅ Топилди: {j_name} ({year}) | DOI: {found_doi}"
                            return {
                                "status": "found",
                                "is_scopus": is_scopus,
                                "matched_title": w.get("title", title),
                                "journal": j_name,
                                "year": year,
                                "doi": found_doi,
                                "badge": badge,
                                "badge_excel": badge_excel
                            }
                except Exception as e:
                    logger.warning(f"OpenAlex DOI lookup failed: {e}")

            # ── СПОСОБ Б: Поиск по названию статьи ──
            clean_title = re.sub(r'[\'\"«»„“”\n\r]', ' ', title).strip()
            search_query = clean_title[:120]
            
            if not search_query:
                return {"status": "not_found", "badge": "⚠️ Ном киритилмаган", "badge_excel": "⚠️ Ном йўқ"}

            encoded_query = urllib.parse.quote(search_query)
            openalex_search_url = f"https://api.openalex.org/works?search={encoded_query}&per-page=3"
            
            async with session.get(openalex_search_url) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    results = data.get("results", [])
                    if results:
                        for best in results:
                            matched_title = best.get("title") or ""
                            # Проверяем степень совпадения названия
                            sim = calculate_similarity(search_query, matched_title)
                            
                            # Если совпадение больше 45% или ключевые слова совпадают
                            if sim >= 0.45 or (len(search_query) > 15 and search_query.lower() in matched_title.lower()) or (len(matched_title) > 15 and matched_title.lower() in search_query.lower()):
                                doi_link = best.get("doi") or ""
                                pub_year = str(best.get("publication_year") or "")
                                
                                prim_loc = best.get("primary_location") or {}
                                source = prim_loc.get("source") or {}
                                journal_name = source.get("display_name") or journal or ""
                                is_scopus = source.get("is_in_scopus", False)
                                
                                clean_doi = doi_link.replace("https://doi.org/", "") if doi_link else ""
                                j_label = f"{journal_name} ({pub_year})" if journal_name and pub_year else (journal_name or "Халқаро нашр")
                                
                                badge = f"✅ Топилди: {j_label}" + (f" | DOI: {clean_doi}" if clean_doi else "")
                                badge_excel = f"✅ Топилди: {j_label}" + (f" | {clean_doi}" if clean_doi else "")
                                
                                return {
                                    "status": "found",
                                    "is_scopus": is_scopus,
                                    "matched_title": matched_title,
                                    "journal": journal_name,
                                    "year": pub_year,
                                    "doi": clean_doi,
                                    "badge": badge,
                                    "badge_excel": badge_excel
                                }

    except asyncio.TimeoutError:
        logger.warning(f"Verification timeout for: {title[:40]}")
        return {
            "status": "timeout",
            "badge": "⏳ База текшируви кутилмоқда",
            "badge_excel": "⏳ Текширувда (Таймаут)"
        }
    except Exception as e:
        logger.error(f"Verification error: {e}")
        return {
            "status": "error",
            "badge": "⚠️ Текширилмади (Қўлда текшириш)",
            "badge_excel": "⚠️ Қўлда текшириш"
        }

    return {
        "status": "not_found",
        "badge": "⚠️ Базадан топилмади (Қўлда текшириш)",
        "badge_excel": "⚠️ Базадан топилмади"
    }
