import { expect, type Locator, type Page, type TestInfo } from '@playwright/test';

export type ThemeTone = 'auto' | 'surface' | 'inverse';

export interface RouteCapture {
  name: string;
  selector: string;
  tone?: ThemeTone;
  minContrast?: number;
  snapshot?: boolean;
}

export interface RouteAuditConfig {
  name: string;
  path: string;
  readySelectors: string[];
  captures: RouteCapture[];
  hoverSelectors?: string[];
}

export interface ThemeIssue {
  category: string;
  severity: string;
  selector: string;
  text?: string;
  contrast?: number;
  color?: string;
  background?: string;
  detail?: string;
  fontSize?: number;
}

export interface ThemeScanResult {
  textIssues: ThemeIssue[];
  surfaceIssues: ThemeIssue[];
  overlayIssues: ThemeIssue[];
}

const SNAPSHOT_MODE = process.env.PLAYWRIGHT_THEME_ENABLE_SNAPSHOTS === '1';

function slugify(value: string): string {
  return String(value || '')
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '')
    .slice(0, 80);
}

export async function waitForAnySelector(
  page: Page,
  selectors: string[],
  timeout: number = 20_000,
): Promise<void> {
  const started = Date.now();
  let lastError: Error | null = null;
  for (;;) {
    for (const selector of selectors) {
      try {
        const locator = page.locator(selector).first();
        if (await locator.isVisible({ timeout: 250 })) {
          return;
        }
      } catch (err) {
        lastError = err instanceof Error ? err : new Error(String(err));
      }
    }
    if (Date.now() - started > timeout) {
      throw lastError || new Error(`Timed out waiting for selectors: ${selectors.join(', ')}`);
    }
    await page.waitForTimeout(250);
  }
}

export async function waitForRouteReady(page: Page, route: RouteAuditConfig): Promise<void> {
  await page.goto(route.path, { waitUntil: 'domcontentloaded' });
  await waitForAnySelector(page, route.readySelectors);
  await page.waitForLoadState('networkidle', { timeout: 10_000 }).catch(() => null);
  await page.waitForTimeout(1_500);
}

export async function discoverAttribute(
  page: Page,
  selectors: string[],
  attributeName: string,
): Promise<string | null> {
  for (const selector of selectors) {
    const locator = page.locator(selector).first();
    if (!(await locator.count())) continue;
    const value = await locator.getAttribute(attributeName).catch(() => null);
    if (value) return value;
  }
  return null;
}

export async function captureRouteArtifacts(
  page: Page,
  route: RouteAuditConfig,
  testInfo: TestInfo,
): Promise<void> {
  const fullPagePath = testInfo.outputPath(`${slugify(route.name)}-full.png`);
  await page.screenshot({ path: fullPagePath, fullPage: true });
  await testInfo.attach(`${route.name}-full`, {
    path: fullPagePath,
    contentType: 'image/png',
  });

  for (const capture of route.captures) {
    const locator = page.locator(capture.selector).first();
    if (!(await locator.count())) continue;
    await expect(locator, `${route.name}:${capture.name} should be visible`).toBeVisible();
    const fileName = `${slugify(route.name)}-${slugify(capture.name)}.png`;
    if (capture.snapshot && SNAPSHOT_MODE) {
      await expect(locator).toHaveScreenshot(fileName, {
        animations: 'disabled',
        caret: 'hide',
        scale: 'css',
        maxDiffPixelRatio: 0.025,
      });
      continue;
    }
    const componentPath = testInfo.outputPath(fileName);
    await locator.screenshot({ path: componentPath });
    await testInfo.attach(`${route.name}-${capture.name}`, {
      path: componentPath,
      contentType: 'image/png',
    });
  }
}

export async function inspectCapture(
  page: Page,
  capture: RouteCapture,
): Promise<{ issues: string[]; selector: string; text: string; contrast: number | null }> {
  return page.evaluate(
    ({ selector, tone, minContrast }) => {
      type Rgba = { r: number; g: number; b: number; a: number };

      const parseColor = (input: string | null | undefined): Rgba | null => {
        const value = String(input || '').trim();
        if (!value || value === 'transparent' || value === 'initial' || value === 'inherit') return null;
        const rgbMatch = value.match(
          /^rgba?\(\s*([0-9.]+)\s*,\s*([0-9.]+)\s*,\s*([0-9.]+)(?:\s*[,/]\s*([0-9.]+))?\s*\)$/i,
        );
        if (rgbMatch) {
          return {
            r: Number(rgbMatch[1]),
            g: Number(rgbMatch[2]),
            b: Number(rgbMatch[3]),
            a: rgbMatch[4] == null ? 1 : Number(rgbMatch[4]),
          };
        }
        const hexMatch = value.match(/^#([0-9a-f]{3,8})$/i);
        if (hexMatch) {
          const hex = hexMatch[1];
          if (hex.length === 3 || hex.length === 4) {
            const r = parseInt(hex[0] + hex[0], 16);
            const g = parseInt(hex[1] + hex[1], 16);
            const b = parseInt(hex[2] + hex[2], 16);
            const a = hex.length === 4 ? parseInt(hex[3] + hex[3], 16) / 255 : 1;
            return { r, g, b, a };
          }
          if (hex.length === 6 || hex.length === 8) {
            const r = parseInt(hex.slice(0, 2), 16);
            const g = parseInt(hex.slice(2, 4), 16);
            const b = parseInt(hex.slice(4, 6), 16);
            const a = hex.length === 8 ? parseInt(hex.slice(6, 8), 16) / 255 : 1;
            return { r, g, b, a };
          }
        }
        return null;
      };

      const composite = (fg: Rgba, bg: Rgba): Rgba => {
        const alpha = fg.a + bg.a * (1 - fg.a);
        if (alpha <= 0) return { r: 255, g: 255, b: 255, a: 0 };
        return {
          r: Math.round((fg.r * fg.a + bg.r * bg.a * (1 - fg.a)) / alpha),
          g: Math.round((fg.g * fg.a + bg.g * bg.a * (1 - fg.a)) / alpha),
          b: Math.round((fg.b * fg.a + bg.b * bg.a * (1 - fg.a)) / alpha),
          a: alpha,
        };
      };

      const gradientColor = (backgroundImage: string): Rgba | null => {
        if (!backgroundImage || backgroundImage === 'none' || !backgroundImage.includes('gradient')) return null;
        const tokens = backgroundImage.match(/rgba?\([^)]+\)|#[0-9a-f]{3,8}/gi) || [];
        if (!tokens.length) return null;
        const colors = tokens
          .map((token) => parseColor(token))
          .filter((entry): entry is Rgba => Boolean(entry));
        if (!colors.length) return null;
        const total = colors.reduce(
          (acc, color) => {
            acc.r += color.r;
            acc.g += color.g;
            acc.b += color.b;
            acc.a += color.a;
            return acc;
          },
          { r: 0, g: 0, b: 0, a: 0 },
        );
        const count = colors.length;
        return {
          r: Math.round(total.r / count),
          g: Math.round(total.g / count),
          b: Math.round(total.b / count),
          a: Math.min(1, total.a / count || 1),
        };
      };

      const luminance = (color: Rgba): number => {
        const channels = [color.r, color.g, color.b].map((channel) => {
          const normalized = channel / 255;
          return normalized <= 0.03928
            ? normalized / 12.92
            : Math.pow((normalized + 0.055) / 1.055, 2.4);
        });
        return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2];
      };

      const contrast = (foreground: Rgba, background: Rgba): number => {
        const fg = composite(foreground, background);
        const lighter = Math.max(luminance(fg), luminance(background));
        const darker = Math.min(luminance(fg), luminance(background));
        return Number(((lighter + 0.05) / (darker + 0.05)).toFixed(2));
      };

      const effectiveBackground = (element: Element): Rgba => {
        let current: Rgba = { r: 255, g: 255, b: 255, a: 1 };
        const htmlStyle = getComputedStyle(document.documentElement);
        const bodyStyle = getComputedStyle(document.body);
        const rootBackgrounds = [
          parseColor(htmlStyle.backgroundColor),
          gradientColor(htmlStyle.backgroundImage),
          parseColor(bodyStyle.backgroundColor),
          gradientColor(bodyStyle.backgroundImage),
        ].filter((entry): entry is Rgba => Boolean(entry));
        for (const background of rootBackgrounds) current = composite(background, current);
        const chain: Element[] = [];
        let node: Element | null = element;
        while (node) {
          chain.unshift(node);
          node = node.parentElement;
        }
        for (const currentNode of chain) {
          const style = getComputedStyle(currentNode);
          const color = parseColor(style.backgroundColor);
          const gradient = gradientColor(style.backgroundImage);
          if (color && color.a > 0.01) current = composite(color, current);
          if (gradient && gradient.a > 0.01) current = composite(gradient, current);
        }
        return current;
      };

      const normalizeText = (input: string | null | undefined): string =>
        String(input || '')
          .replace(/\s+/g, ' ')
          .trim();

      const cssPath = (element: Element): string => {
        const segments: string[] = [];
        let current: Element | null = element;
        while (current && segments.length < 4) {
          let part = current.tagName.toLowerCase();
          if (current.id) {
            part += `#${current.id}`;
            segments.unshift(part);
            break;
          }
          const classes = Array.from(current.classList).slice(0, 2);
          if (classes.length) part += `.${classes.join('.')}`;
          segments.unshift(part);
          current = current.parentElement;
        }
        return segments.join(' > ');
      };

      const element = document.querySelector(selector);
      if (!element) {
        return { issues: ['missing'], selector, text: '', contrast: null };
      }
      const rect = element.getBoundingClientRect();
      const style = getComputedStyle(element);
      const text = normalizeText((element as HTMLElement).innerText || element.textContent);
      const issues: string[] = [];
      if (style.display === 'none' || style.visibility === 'hidden' || Number(style.opacity || 1) <= 0.05) {
        issues.push('not-visible');
      }
      if (rect.width < 4 || rect.height < 4) issues.push('collapsed');
      if (!text) issues.push('empty-text');

      const foreground = parseColor(style.color);
      const background = effectiveBackground(element);
      const ratio = foreground ? contrast(foreground, background) : null;
      const required = Number(minContrast || 3.2);
      if (!foreground) {
        issues.push('missing-color');
      } else {
        if (foreground.a < 0.7) issues.push('text-transparent');
        if (ratio != null && ratio < required) issues.push(`contrast-${ratio}`);
        const lum = luminance(foreground);
        if ((tone || 'auto') === 'inverse' && lum < 0.62 && (ratio == null || ratio < required)) {
          issues.push('expected-inverse-text');
        }
        if ((tone || 'auto') === 'surface' && lum > 0.76 && (ratio == null || ratio < required)) {
          issues.push('expected-surface-text');
        }
      }
      return {
        issues,
        selector: cssPath(element),
        text,
        contrast: ratio,
      };
    },
    capture,
  );
}

export async function scanThemeIssues(page: Page): Promise<ThemeScanResult> {
  return page.evaluate(() => {
    type Rgba = { r: number; g: number; b: number; a: number };

    const parseColor = (input: string | null | undefined): Rgba | null => {
      const value = String(input || '').trim();
      if (!value || value === 'transparent' || value === 'initial' || value === 'inherit') return null;
      const rgbMatch = value.match(
        /^rgba?\(\s*([0-9.]+)\s*,\s*([0-9.]+)\s*,\s*([0-9.]+)(?:\s*[,/]\s*([0-9.]+))?\s*\)$/i,
      );
      if (rgbMatch) {
        return {
          r: Number(rgbMatch[1]),
          g: Number(rgbMatch[2]),
          b: Number(rgbMatch[3]),
          a: rgbMatch[4] == null ? 1 : Number(rgbMatch[4]),
        };
      }
      const hexMatch = value.match(/^#([0-9a-f]{3,8})$/i);
      if (hexMatch) {
        const hex = hexMatch[1];
        if (hex.length === 3 || hex.length === 4) {
          const r = parseInt(hex[0] + hex[0], 16);
          const g = parseInt(hex[1] + hex[1], 16);
          const b = parseInt(hex[2] + hex[2], 16);
          const a = hex.length === 4 ? parseInt(hex[3] + hex[3], 16) / 255 : 1;
          return { r, g, b, a };
        }
        if (hex.length === 6 || hex.length === 8) {
          const r = parseInt(hex.slice(0, 2), 16);
          const g = parseInt(hex.slice(2, 4), 16);
          const b = parseInt(hex.slice(4, 6), 16);
          const a = hex.length === 8 ? parseInt(hex.slice(6, 8), 16) / 255 : 1;
          return { r, g, b, a };
        }
      }
      return null;
    };

    const composite = (fg: Rgba, bg: Rgba): Rgba => {
      const alpha = fg.a + bg.a * (1 - fg.a);
      if (alpha <= 0) return { r: 255, g: 255, b: 255, a: 0 };
      return {
        r: Math.round((fg.r * fg.a + bg.r * bg.a * (1 - fg.a)) / alpha),
        g: Math.round((fg.g * fg.a + bg.g * bg.a * (1 - fg.a)) / alpha),
        b: Math.round((fg.b * fg.a + bg.b * bg.a * (1 - fg.a)) / alpha),
        a: alpha,
      };
    };

    const gradientColor = (backgroundImage: string): Rgba | null => {
      if (!backgroundImage || backgroundImage === 'none' || !backgroundImage.includes('gradient')) return null;
      const tokens = backgroundImage.match(/rgba?\([^)]+\)|#[0-9a-f]{3,8}/gi) || [];
      if (!tokens.length) return null;
      const colors = tokens
        .map((token) => parseColor(token))
        .filter((entry): entry is Rgba => Boolean(entry));
      if (!colors.length) return null;
      const total = colors.reduce(
        (acc, color) => {
          acc.r += color.r;
          acc.g += color.g;
          acc.b += color.b;
          acc.a += color.a;
          return acc;
        },
        { r: 0, g: 0, b: 0, a: 0 },
      );
      const count = colors.length;
      return {
        r: Math.round(total.r / count),
        g: Math.round(total.g / count),
        b: Math.round(total.b / count),
        a: Math.min(1, total.a / count || 1),
      };
    };

    const luminance = (color: Rgba): number => {
      const channels = [color.r, color.g, color.b].map((channel) => {
        const normalized = channel / 255;
        return normalized <= 0.03928
          ? normalized / 12.92
          : Math.pow((normalized + 0.055) / 1.055, 2.4);
      });
      return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2];
    };

    const contrast = (foreground: Rgba, background: Rgba): number => {
      const fg = composite(foreground, background);
      const lighter = Math.max(luminance(fg), luminance(background));
      const darker = Math.min(luminance(fg), luminance(background));
      return Number(((lighter + 0.05) / (darker + 0.05)).toFixed(2));
    };

    const normalizeText = (input: string | null | undefined): string =>
      String(input || '')
        .replace(/\s+/g, ' ')
        .trim();

    const cssPath = (element: Element): string => {
      const segments: string[] = [];
      let current: Element | null = element;
      while (current && segments.length < 5) {
        let part = current.tagName.toLowerCase();
        if (current.id) {
          part += `#${current.id}`;
          segments.unshift(part);
          break;
        }
        const classes = Array.from(current.classList).slice(0, 2);
        if (classes.length) part += `.${classes.join('.')}`;
        segments.unshift(part);
        current = current.parentElement;
      }
      return segments.join(' > ');
    };

    const visible = (element: Element): boolean => {
      const htmlElement = element as HTMLElement;
      const style = getComputedStyle(element);
      const rect = element.getBoundingClientRect();
      if (style.display === 'none' || style.visibility === 'hidden') return false;
      if (Number(style.opacity || 1) <= 0.04) return false;
      if (rect.width < 2 || rect.height < 2) return false;
      if (htmlElement.closest('[disabled], [aria-disabled="true"], .disabled')) return false;
      return true;
    };

    const effectiveBackground = (element: Element): Rgba => {
      let current: Rgba = { r: 255, g: 255, b: 255, a: 1 };
      const htmlStyle = getComputedStyle(document.documentElement);
      const bodyStyle = getComputedStyle(document.body);
      const rootBackgrounds = [
        parseColor(htmlStyle.backgroundColor),
        gradientColor(htmlStyle.backgroundImage),
        parseColor(bodyStyle.backgroundColor),
        gradientColor(bodyStyle.backgroundImage),
      ].filter((entry): entry is Rgba => Boolean(entry));
      for (const background of rootBackgrounds) current = composite(background, current);
      const chain: Element[] = [];
      let node: Element | null = element;
      while (node) {
        chain.unshift(node);
        node = node.parentElement;
      }
      for (const currentNode of chain) {
        const style = getComputedStyle(currentNode);
        const color = parseColor(style.backgroundColor);
        const gradient = gradientColor(style.backgroundImage);
        if (color && color.a > 0.01) current = composite(color, current);
        if (gradient && gradient.a > 0.01) current = composite(gradient, current);
      }
      return current;
    };

    const textIssues: ThemeIssue[] = [];
    const dedupe = new Set<string>();
    const candidateSelectors = [
      'main h1',
      'main h2',
      'main h3',
      'main h4',
      'main p',
      'main span',
      'main a',
      'main button',
      'main label',
      'main th',
      'main td',
      'main small',
      'main strong',
      'main .text-muted',
      'main [class*="title"]',
      'main [class*="subtitle"]',
      'main [class*="headline"]',
      'main [class*="eyebrow"]',
      'main [class*="label"]',
      'main [class*="meta"]',
      'main [class*="detail"]',
    ];
    const candidateNodes = Array.from(document.querySelectorAll(candidateSelectors.join(',')));
    for (const element of candidateNodes) {
      if (!visible(element)) continue;
      if (element.closest('.visually-hidden, [aria-hidden="true"]')) continue;
      if (element.closest('[disabled], [aria-disabled="true"], .disabled')) continue;
      if (element.closest('.suppliers-hero, .labor-hero')) continue;
      const style = getComputedStyle(element);
      if (style.pointerEvents === 'none' && Number(style.opacity || 1) < 0.5) continue;
      const ownText = normalizeText(
        Array.from(element.childNodes)
          .filter((node) => node.nodeType === Node.TEXT_NODE)
          .map((node) => node.textContent || '')
          .join(' '),
      );
      const text = ownText || normalizeText((element as HTMLElement).innerText || element.textContent);
      if (!text || text.length < 2) continue;
      if (/^(loading|n\/a|--|—)$/i.test(text)) continue;
      const fg = parseColor(style.color);
      if (!fg) continue;
      const bg = effectiveBackground(element);
      const ratio = contrast(fg, bg);
      const fontSize = Number.parseFloat(style.fontSize || '0') || 0;
      const fontWeight = Number.parseInt(style.fontWeight || '400', 10) || 400;
      const required = fontSize >= 24 || (fontSize >= 18 && fontWeight >= 600) ? 3.0 : 4.0;
      const fgLum = luminance(fg);
      const bgLum = luminance(bg);
      let category = '';
      let severity = '';
      let detail = '';
      if (fg.a < 0.7) {
        category = 'transparent_text';
        severity = 'high';
        detail = 'Computed text alpha is too low.';
      } else if (ratio < required && fgLum > 0.84 && bgLum > 0.82) {
        category = 'white_on_white';
        severity = 'critical';
        detail = `Very light foreground on a very light surface (${ratio}).`;
      } else if (ratio < required && fgLum < 0.28 && bgLum < 0.32) {
        category = 'dark_on_dark';
        severity = 'critical';
        detail = `Very dark foreground on a very dark surface (${ratio}).`;
      } else if (ratio < required && style.opacity && Number(style.opacity) < 0.92) {
        category = 'parent_opacity';
        severity = 'high';
        detail = `Text inherits lowered opacity (${style.opacity}) and only reaches ${ratio}.`;
      } else if (ratio < required && /muted/i.test(element.className || '')) {
        category = 'muted_overuse';
        severity = 'medium';
        detail = `Muted treatment reduces contrast to ${ratio}.`;
      } else if (ratio < required) {
        category = 'low_contrast_text';
        severity = ratio < 2.6 ? 'critical' : 'high';
        detail = `Contrast ${ratio} is below threshold ${required}.`;
      }
      if (!category) continue;
      const key = `${category}|${cssPath(element)}|${text.slice(0, 72)}`;
      if (dedupe.has(key)) continue;
      dedupe.add(key);
      textIssues.push({
        category,
        severity,
        selector: cssPath(element),
        text: text.slice(0, 140),
        contrast: ratio,
        color: style.color,
        background: `rgba(${bg.r}, ${bg.g}, ${bg.b}, ${bg.a.toFixed(2)})`,
        detail,
        fontSize,
      });
    }

    const surfaceIssues: ThemeIssue[] = [];
    const surfaceCandidates = Array.from(
      document.querySelectorAll(
        [
          'main .card',
          'main .page-card',
          'main .table-responsive',
          'main .datatable',
          'main .filters-panel',
          'main [class*="surface"]',
          'main .overview-panel',
          'main .overview-shell-card',
          'main .section-shell',
        ].join(','),
      ),
    ).slice(0, 80);
    const bodyBackground = effectiveBackground(document.body);
    for (const surface of surfaceCandidates) {
      if (!visible(surface)) continue;
      if (surface.matches('.card-header, .card-footer, .panel-header, .section-shell-header, .hero-side-header')) {
        continue;
      }
      const style = getComputedStyle(surface);
      const background = effectiveBackground(surface);
      const surfaceContrast = contrast(background, bodyBackground);
      const borderColor = parseColor(style.borderColor);
      const borderContrast = borderColor ? contrast(borderColor, background) : 1;
      const hasShadow = style.boxShadow && style.boxShadow !== 'none';
      const hasBackdrop = style.backdropFilter && style.backdropFilter !== 'none';
      if (surfaceContrast < 1.1 && borderContrast < 1.08 && !hasShadow) {
        surfaceIssues.push({
          category: 'surface_blending',
          severity: 'high',
          selector: cssPath(surface),
          contrast: surfaceContrast,
          background: `rgba(${background.r}, ${background.g}, ${background.b}, ${background.a.toFixed(2)})`,
          detail: 'Card or panel does not separate from the page background.',
        });
      }
      if (Number(style.opacity || 1) < 0.94) {
        surfaceIssues.push({
          category: 'surface_opacity',
          severity: 'medium',
          selector: cssPath(surface),
          detail: `Surface opacity is ${style.opacity}.`,
        });
      }
      if (hasBackdrop) {
        surfaceIssues.push({
          category: 'backdrop_filter',
          severity: 'medium',
          selector: cssPath(surface),
          detail: `Surface uses backdrop-filter (${style.backdropFilter}).`,
        });
      }
    }

    const overlayIssues: ThemeIssue[] = [];
    const viewportArea = Math.max(1, window.innerWidth * window.innerHeight);
    const overlayCandidates = Array.from(document.body.querySelectorAll('*')).slice(0, 1500);
    for (const node of overlayCandidates) {
      if (!visible(node)) continue;
      const style = getComputedStyle(node);
      if (!['fixed', 'absolute', 'sticky'].includes(style.position)) continue;
      const rect = node.getBoundingClientRect();
      const area = rect.width * rect.height;
      const background = parseColor(style.backgroundColor) || gradientColor(style.backgroundImage);
      const zIndex = Number.parseInt(style.zIndex || '0', 10) || 0;
      const className = String((node as HTMLElement).className || '');
      const looksLikeOverlay =
        /overlay|backdrop|sheet|drawer|modal|loading|scrim/i.test(className) ||
        /backdrop/i.test(node.getAttribute('role') || '');
      if (!looksLikeOverlay) continue;
      if (zIndex < 200) continue;
      if (area / viewportArea < 0.35) continue;
      if (!background || background.a < 0.08) continue;
      overlayIssues.push({
        category: 'stale_overlay',
        severity: 'critical',
        selector: cssPath(node),
        background: style.backgroundColor,
        detail: 'Overlay-like layer remains visible across a large share of the viewport.',
      });
    }

    const sortBySeverity = (issues: ThemeIssue[]) =>
      issues.sort((left, right) => {
        const score = (issue: ThemeIssue) =>
          issue.severity === 'critical' ? 0 : issue.severity === 'high' ? 1 : 2;
        return score(left) - score(right);
      });

    return {
      textIssues: sortBySeverity(textIssues).slice(0, 40),
      surfaceIssues: sortBySeverity(surfaceIssues).slice(0, 20),
      overlayIssues: sortBySeverity(overlayIssues).slice(0, 10),
    };
  });
}

export async function auditHoverSelectors(
  page: Page,
  selectors: string[],
): Promise<Array<{ selector: string; contrast: number | null; issues: string[] }>> {
  const findings: Array<{ selector: string; contrast: number | null; issues: string[] }> = [];
  for (const selector of selectors) {
    const locator = page.locator(selector).first();
    if (!(await locator.count())) continue;
    if (!(await locator.isVisible().catch(() => false))) continue;
    await locator.hover();
    await page.waitForTimeout(120);
    const result = await inspectCapture(page, {
      name: selector,
      selector,
      tone: 'auto',
      minContrast: 3,
    });
    findings.push({
      selector,
      contrast: result.contrast,
      issues: result.issues,
    });
  }
  return findings;
}
