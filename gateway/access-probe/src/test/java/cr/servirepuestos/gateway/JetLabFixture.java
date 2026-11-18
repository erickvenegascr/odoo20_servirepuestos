package cr.servirepuestos.gateway;

import com.google.gson.GsonBuilder;
import com.healthmarketscience.jackcess.*;
import java.io.File;
import java.math.BigDecimal;
import java.time.LocalDateTime;
import java.util.*;

/** Synthetic data only. Jackcess writes fixtures, never the gateway's updates. */
public final class JetLabFixture {
    public static void main(String[] args) throws Exception {
        File file = new File(args[1]);
        if ("create".equals(args[0])) {
            if (file.exists()) { throw new IllegalArgumentException("Fixture destination exists"); }
            try (Database db = DatabaseBuilder.create(Database.FileFormat.V2000, file)) {
                db.setDateTimeType(DateTimeType.LOCAL_DATE_TIME);
                Table table = new TableBuilder("PGPropiedades")
                    .addColumn(new ColumnBuilder("Nombre", DataType.TEXT).setLengthInUnits(50).setCompressedUnicode(true))
                    .addColumn(new ColumnBuilder("Valor", DataType.MEMO).setCompressedUnicode(true))
                    .addColumn(new ColumnBuilder("Guard", DataType.LONG))
                    .addIndex(new IndexBuilder("PrimaryKey").addColumns("Nombre").setPrimaryKey()).toTable(db);
                table.addRow("CounterFixture", "100", 777);
                table.addRow("OtherFixture", "666", 888);
                Table catalog = new TableBuilder("Catalog")
                    .addColumn(new ColumnBuilder("Id", DataType.LONG))
                    .addColumn(new ColumnBuilder("Description", DataType.TEXT).setLengthInUnits(100))
                    .addColumn(new ColumnBuilder("Price", DataType.MONEY))
                    .addColumn(new ColumnBuilder("Moment", DataType.SHORT_DATE_TIME))
                    .addColumn(new ColumnBuilder("PrivateToken", DataType.TEXT).setLengthInUnits(50))
                    .addIndex(new IndexBuilder("PrimaryKey").addColumns("Id").setPrimaryKey()).toTable(db);
                catalog.addRow(1, "Artículo ñ", new BigDecimal("123.4500"), LocalDateTime.of(2026,11,18,9,30), "private-canary");
                catalog.addRow(2, null, new BigDecimal("0.0100"), null, "private-canary-2");
                db.createLinkedTable("LinkedProducts", "MISSING_DO_NOT_OPEN.mdb", "Products");
            }
        } else if ("read".equals(args[0])) {
            try (Database db = new DatabaseBuilder(file).setReadOnly(true).open()) {
                Table table = db.getTable("PGPropiedades");
                List<Row> rows = new ArrayList<>();
                for (Row row : table) { rows.add(row); }
                Cursor cursor = table.getPrimaryKeyIndex().newCursor().toCursor();
                if (!cursor.findFirstRow(Collections.singletonMap("Nombre", "CounterFixture"))) {
                    throw new IllegalStateException("Primary key readback failed");
                }
                System.out.println(new GsonBuilder().serializeNulls().create().toJson(rows));
            }
        } else { throw new IllegalArgumentException("Unknown fixture action"); }
    }
}
