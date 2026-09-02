import { useMemo, useRef, useState, type ReactNode } from "react";
import { Table } from "antd";
import type { ColumnsType, TableProps } from "antd/es/table";

interface ResizableTableProps<Row extends object> {
  columns: ColumnsType<Row>;
  dataSource: TableProps<Row>["dataSource"];
  pagination?: TableProps<Row>["pagination"];
  rowClassName?: TableProps<Row>["rowClassName"];
  size?: TableProps<Row>["size"];
}

interface ResizeState {
  key: string;
  startX: number;
  startWidth: number;
}

function columnKey(column: ColumnsType<never>[number], index: number): string {
  return String(column.key || column.dataIndex || index);
}

export function ResizableTable<Row extends object>({
  columns,
  dataSource,
  pagination,
  rowClassName,
  size = "small"
}: ResizableTableProps<Row>) {
  const defaults = useMemo(() => {
    const next: Record<string, number> = {};
    columns.forEach((column, index) => {
      const width = typeof column.width === "number" ? column.width : 220;
      next[columnKey(column as ColumnsType<never>[number], index)] = width;
    });
    return next;
  }, [columns]);
  const [widths, setWidths] = useState<Record<string, number>>(defaults);
  const dragRef = useRef<ResizeState | null>(null);

  const sizedColumns = columns.map((column, index) => {
    const key = columnKey(column as ColumnsType<never>[number], index);
    const width = widths[key] ?? (typeof column.width === "number" ? column.width : 220);
    const title = column.title;
    return {
      ...column,
      width,
      ellipsis: false,
      title: (
        <div className="resizable-th">
          <span className="resizable-th-label">{title as ReactNode}</span>
          <button
            aria-label={`拖动调整列宽`}
            className="col-resize-handle"
            onClick={(event) => event.stopPropagation()}
            onPointerDown={(event) => {
              event.preventDefault();
              event.stopPropagation();
              event.currentTarget.setPointerCapture(event.pointerId);
              dragRef.current = { key, startX: event.clientX, startWidth: width };
            }}
            onPointerMove={(event) => {
              const drag = dragRef.current;
              if (!drag || drag.key !== key || !event.currentTarget.hasPointerCapture(event.pointerId)) {
                return;
              }
              const nextWidth = Math.min(900, Math.max(72, drag.startWidth + (event.clientX - drag.startX)));
              setWidths((previous) => ({ ...previous, [key]: nextWidth }));
            }}
            onPointerUp={(event) => {
              if (event.currentTarget.hasPointerCapture(event.pointerId)) {
                event.currentTarget.releasePointerCapture(event.pointerId);
              }
              dragRef.current = null;
            }}
            type="button"
          />
        </div>
      )
    };
  });

  const scrollX = sizedColumns.reduce((sum, column) => sum + Number(column.width || 0), 0);

  return (
    <Table<Row>
      className="resizable-table"
      columns={sizedColumns}
      dataSource={dataSource}
      pagination={pagination}
      rowClassName={rowClassName}
      scroll={{ x: Math.max(scrollX, 960) }}
      size={size}
      tableLayout="fixed"
    />
  );
}
