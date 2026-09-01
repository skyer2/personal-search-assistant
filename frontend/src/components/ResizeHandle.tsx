import { useRef, type PointerEvent } from "react";

interface ResizeHandleProps {
  axis: "x" | "y";
  label: string;
  max: number;
  min: number;
  onChange: (value: number) => void;
  value: number;
}

export function ResizeHandle({ axis, label, max, min, onChange, value }: ResizeHandleProps) {
  const drag = useRef<{ origin: number; start: number } | null>(null);

  function pointerPosition(event: PointerEvent<HTMLButtonElement>): number {
    return axis === "x" ? event.clientX : event.clientY;
  }

  function handlePointerDown(event: PointerEvent<HTMLButtonElement>) {
    event.preventDefault();
    event.currentTarget.setPointerCapture(event.pointerId);
    drag.current = { origin: pointerPosition(event), start: value };
  }

  function handlePointerMove(event: PointerEvent<HTMLButtonElement>) {
    if (!drag.current || !event.currentTarget.hasPointerCapture(event.pointerId)) {
      return;
    }
    const delta = pointerPosition(event) - drag.current.origin;
    const next = Math.min(max, Math.max(min, drag.current.start + delta));
    onChange(next);
  }

  function handlePointerUp(event: PointerEvent<HTMLButtonElement>) {
    drag.current = null;
    if (event.currentTarget.hasPointerCapture(event.pointerId)) {
      event.currentTarget.releasePointerCapture(event.pointerId);
    }
  }

  return (
    <button
      aria-label={label}
      aria-orientation={axis === "x" ? "vertical" : "horizontal"}
      aria-valuemax={max}
      aria-valuemin={min}
      aria-valuenow={Math.round(value)}
      className={`resize-handle resize-handle--${axis}`}
      onPointerDown={handlePointerDown}
      onPointerMove={handlePointerMove}
      onPointerUp={handlePointerUp}
      role="separator"
      type="button"
    />
  );
}
