// D21: a full-colour logo ships one file per theme. Render both and let the design-system rule (.gdg-logo-light / .gdg-logo-dark in
// bundle.css) show the one of the current data-theme. No hooks and no CSS import: it renders the same on the server, in the
// browser and in node tests.
export function ThemeLogo({
  light,
  dark,
  alt,
  className,
  width,
  height,
}: Readonly<{ light: string; dark: string; alt: string; className?: string; width?: number; height?: number }>) {
  const cls = (theme: string) => [className, theme].filter(Boolean).join(" ");
  return (
    <>
      <img className={cls("gdg-logo-light")} src={light} alt={alt} width={width} height={height} />
      <img className={cls("gdg-logo-dark")} src={dark} alt={alt} width={width} height={height} />
    </>
  );
}
