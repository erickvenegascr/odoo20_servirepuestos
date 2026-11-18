package cr.servirepuestos.gateway;

import com.google.gson.Gson;
import com.healthmarketscience.jackcess.ColumnBuilder;
import com.healthmarketscience.jackcess.DataType;
import com.healthmarketscience.jackcess.Database;
import com.healthmarketscience.jackcess.DatabaseBuilder;
import com.healthmarketscience.jackcess.Table;
import com.healthmarketscience.jackcess.TableBuilder;
import java.nio.file.Files;
import java.nio.file.Path;
import java.security.MessageDigest;
import java.util.Arrays;
import java.util.Map;
import java.util.stream.Stream;
import org.junit.Test;
import static org.junit.Assert.*;

public class AccessProbeTest {
    @Test public void readsRealJetFileWithoutChangingIt() throws Exception {
        Path directory = Files.createTempDirectory("access-probe-");
        Path file = directory.resolve("Fixture.mdb");
        try {
            try (Database db = DatabaseBuilder.create(Database.FileFormat.V2000, file.toFile())) {
                db.getUserDefinedProperties().put("UltArticulo", 123);
                db.getUserDefinedProperties().save();
                Table table = new TableBuilder("PGPropiedades")
                    .addColumn(new ColumnBuilder("Nombre", DataType.TEXT).setLengthInUnits(50))
                    .addColumn(new ColumnBuilder("Valor", DataType.MEMO)).toTable(db);
                table.addRow("ArticuloSecuencial", "True");
                table.addRow("ProxNumArticulo", "124");
                table.addRow("ArticuloToken", "private-canary");
            }
            byte[] before = MessageDigest.getInstance("SHA-256").digest(Files.readAllBytes(file));
            Map<String, Object> result = AccessProbe.inspect(file);
            byte[] after = MessageDigest.getInstance("SHA-256").digest(Files.readAllBytes(file));
            assertArrayEquals(before, after);
            assertEquals("inspected", result.get("status"));
            assertEquals(false, result.get("business_writes"));
            assertEquals(false, result.get("counter_confirmed"));
            String json = new Gson().toJson(result);
            assertTrue(json.contains("UltArticulo")); assertTrue(json.contains("ProxNumArticulo"));
            assertFalse(json.contains("private-canary")); assertFalse(json.contains("ArticuloToken"));
            try (Stream<Path> files = Files.list(directory)) { assertEquals(1, files.count()); }
        } finally {
            Files.deleteIfExists(file); Files.deleteIfExists(directory);
        }
    }

    @Test public void rejectsUnrelatedAndSensitiveNames() {
        for (String name : Arrays.asList("Password", "ArticuloClave", "Cliente", "ArticuloToken")) {
            assertFalse(AccessProbe.relevant(name));
        }
        assertTrue(AccessProbe.relevant("Artículo secuencial"));
    }
}
