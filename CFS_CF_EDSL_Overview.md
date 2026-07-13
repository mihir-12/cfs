# cFS / CF and the EDSL Model — Project Overview

This document explains the domain behind the files in this project:

1. What **cFS** (NASA Core Flight System) is.
2. What the **CF** (CFDP File Transfer) application is.
3. What the **EDSL** files are and what each one contains.
4. A reference section describing **each data structure** defined and used across the EDSL files.

It is intended as background for the semantic-similarity work in `main.py`, which matches
elements of the EDSL model against the prose requirements in `cf_FunctionalRequirements.csv`.

---

## 1. What is cFS?

**cFS (Core Flight System)** is NASA's open-source **flight-software framework**. It is not a
single program; it is a runtime plus a library of reusable "applications" (apps) that run on
spacecraft flight computers. cFS provides a common architecture so that missions can reuse
well-tested components instead of rewriting flight software from scratch.

The core of cFS is **cFE (Core Flight Executive)**, which provides a small set of foundational
services that every app builds on:

- **SB — Software Bus:** a publish/subscribe message router. Apps talk to each other by
sending/receiving messages identified by a **Message ID (MsgId)** rather than by calling
each other directly.
- **TBL — Table Services:** load/dump/validate/activate configuration tables at runtime.
- **ES — Executive Services:** app lifecycle, memory pools, resource IDs.
- **EVS — Event Services:** event/log messages (info, error, debug, critical).
- **TIME — Time Services:** spacecraft time management.

Apps are configured for a specific mission through **configuration constants** and **topic IDs**
(message routing identifiers). Everything is built around CCSDS space packet standards.

## 2. What is CF?

**CF** is a single cFS **application**: the **CFDP File Transfer app**. CFDP (the CCSDS
**File Delivery Protocol**) is the standard for reliably moving files between a spacecraft and
the ground.

CF supports two service modes defined by CFDP:

- **Class 1 — Unacknowledged** ("unreliable"): used for non-critical data or when there is no
return data path.
- **Class 2 — Acknowledged** ("reliable"): uses ACKs/NAKs and retransmission for critical data
over a bi-directional link.

Key CF concepts:

- **Channel:** an independent output path (this project is configured for `NUM_CHANNELS = 2`).
Each channel has its own queues and its own Software Bus Message ID.
- **Transaction:** a single file transfer (send or receive), identified by an Entity ID +
transaction sequence number.
- **Playback / Polling directory:** CF can transfer a specific file, play back all files in a
directory on command, or automatically poll configured directories for files to send.
- **Queues:** pending, active (TX/RX), and history queues track transactions.
- **Freeze/Thaw, Suspend/Resume, Cancel/Abandon:** operational commands to control transactions
and channels (e.g., to align transfers with a ground-contact schedule).
- **Housekeeping (HK) telemetry** and **End-of-Transaction (EOT) telemetry** report status.

The prose requirements for CF live in `cf_FunctionalRequirements.csv` (IDs like `CF1000`,
`CF3000`, `CF5002`). The EDSL files below are the **formal, machine-readable model** of the same
application — its commands, telemetry, tables, and configuration.

---



## 3. What is EDSL and what does each file contain?

**EDSL** here is an *Electronic Data Sheet*-style domain-specific language used in the cFS
ecosystem to formally describe an app's interfaces: its data types, message/packet layouts,
commands, telemetry, tables, configuration constants, interfaces, and components. Where the CSV
says in prose *"CF shall issue an End-of-Transaction message containing …"*, the EDSL declares
the exact structure of that message.

Common building blocks used in every `.edsl` file:

- `package` — names the module (e.g., `package CF "CFDP"`).
- `from X import *` — pulls in definitions from another package (the dependency layering).
- `dataTypeSet { ... }` — defines data types: integers, enums, arrays, strings, and
`container`s (structured records / packets).
- `interfaceSet { ... }` — defines abstract interfaces (e.g., `Telecommand`, `Telemetry`).
- `componentSet { ... }` — defines components and which interfaces they provide/require.
- `metadata { ... }` — configuration values, or cFS binding info (app name, MsgIDs, etc.).
- `${NAME}` — a substitution referencing a constant defined in `CONFIG` or `CFE_TOPICIDS`.

The files form a **dependency stack** (each imports the ones below it):

```
CONFIG            (global constants)        CFE_TOPICIDS   (message topic IDs)
        \\                                   /
         BASE_TYPES  (primitive types: uint8..uint64, strings, float)
              |
         CFE_HDR     (CCSDS message headers)
              |
         CFE_SB      (Software Bus: MsgId, Telecommand/Telemetry interfaces)
              |
         CFE_TBL     (Table Services: load/dump/validate/activate)
              |
         CF          (the CFDP File Transfer application model)
```



### 3.1 `CONFIG.edsl`

Global, mission-wide **configuration constants** for *all* cFS apps, grouped by app category
(`HK`, `SC`, `MM`, `CF`, `CS`, `DS`, `FM`, `HS`, `LC`, …, plus a large `CFE_MISSION` block).
It holds no data types — only `IntegerValue`/`StringValue` design parameters. The `CF` block sets
CF's sizing knobs, and `CFE_MISSION` sets things like byte order, string lengths, spacecraft ID,
and Software Bus limits. Other files reference these via `${...}` (e.g., `${NUM_CHANNELS}`,
`${CORE_API_MAX_PATH_LEN}`, `${DATA_BYTE_ORDER}`).

### 3.2 `CFE_TOPICIDS.edsl`

Global **topic ID (message routing ID) assignments** for every app in the system. It computes
base ranges for commands vs. telemetry and then assigns each app's specific IDs (e.g.,
`CF_CMD_TOPICID`, `CF_HK_TLM_TOPICID`, `CF_EOT_TLM_TOPICID`, `CF_CH0_RX_TOPICID`,
`CF_CH0_TX_TOPICID`). Like `CONFIG`, it is pure `metadata` constants consumed elsewhere.

### 3.3 `BASE_TYPES.edsl`

The **primitive type library** used everywhere. Defines sized integers (`int8`–`int64`,
`uint8`–`uint64`), floating point (`float`, `double`), a 1-bit `StatusBit` boolean, a
memory-reference integer, and fixed-length string types (`ApiName`, `PathName`, `FileName`).
Encoding, byte order, and string lengths are pulled from `CONFIG`.

### 3.4 `CFE_HDR.edsl`

The **CCSDS message header formats**. Defines the base `Message` (wrapping a `SpacePacketBasic`),
the command and telemetry secondary headers, and the composite `CommandHeader` and
`TelemetryHeader`. Every command/telemetry packet in the other files `extends` one of these.

### 3.5 `CFE_SB.edsl`

The **Software Bus** package. Defines the `MsgId`/`MsgIdValue` used for pub/sub routing, quality-
of-service enums, pipe/route structures, SB housekeeping and statistics telemetry, and — most
importantly for other apps — the abstract `Telecommand` and `Telemetry` interfaces and the
generic `Application` component pattern that CF reuses.

### 3.6 `CFE_TBL.edsl`

The **Table Services** package. Defines the table file header formats and the standard table
management commands (`LoadCmd`, `DumpCmd`, `ValidateCmd`, `ActivateCmd`, `DumpRegistryCmd`, etc.),
plus table housekeeping and registry telemetry, and a `Table` interface with `load`/`dump`
operations. CF uses this to load and validate its configuration table.

### 3.7 `CF.edsl`

The **CF application model itself** — the formal counterpart to `cf_FunctionalRequirements.csv`.
It imports SB, HDR, TBL, CONFIG, and TOPICIDS, then defines:

- CF-specific value types and enums (`EntityId`, `TransactionSeq`, `ChannelId`, `CFDP`, `Reset`, …).
- The configuration table structures (`ConfigTable`, `ChannelConfig`, `PollDir`).
- Housekeeping and End-of-Transaction telemetry (`HkPacket`, `EotPacket`, and their payloads).
- Command payloads and the full command set (No-Op, Reset, Transfer File, Playback Directory,
Freeze/Thaw, Suspend/Resume, Cancel/Abandon, Get/Set param, polling, engine enable/disable, …).
- The CF `Application` component and its cFS bindings (MsgIDs) in `metadata`.

---



## 4. EDSL constructs explained

This section explains the main building blocks you will see inside the `.edsl` files —
`dataTypeSet`, `container`s, interfaces, and applications (components) — and how they fit together.

A helpful mental model:

- `dataTypeSet` answers *"what does the data look like?"* (types, records, messages).
- **interfaces** answer *"what contracts/operations exist?"* (abstract ways components talk).
- **components / applications** answer *"what software units exist and how are they wired?"*



### 4.1 `dataTypeSet`

`dataTypeSet { ... }` is the section of a package that **defines the data types** available in that
package. Nothing here is live state — these are blueprints (like `typedef`s and `struct`
definitions in C). Other packages can reuse them via `from <Package>.dataTypeSet import *`.

It contains a mix of:

- **Scalar types** — `IntegerDataType`, `FloatDataType`, `BooleanDataType` (an encoding, a bit
size, and an optional range). Example: `uint16` in `BASE_TYPES.edsl`.
- `StringDataType` — fixed-length text (e.g., `PathName`, length `CORE_API_MAX_PATH_LEN`).
- `EnumeratedDataType` — a named set of integer constants (e.g., CF's `CFDP { CLASS_1, CLASS_2 }`).
- `ArrayDataType` — a fixed-length array of another type.
- `container` — a structured record (see below).



### 4.2 `container`

A `container` is a **structured record** — a named group of typed fields, like a C `struct`. It is
the single most common construct, because every message, packet, table, and payload layout is
described as a container. **A** `container` **is itself a data type, so it is declared *inside* the**
`dataTypeSet { ... }` **block**, alongside the scalar types, enums, and arrays (not in `interfaceSet`
or `componentSet`). Each field is written as `<type> <name>`:

```286:291:CF.edsl
    // Transaction command structure
    container Transaction_Payload {
        uint32 ts
        uint32 eid
        uint8 chan
    }
```

Key points about containers:

- **They are passive data.** A container describes *what bytes look like*; it does not *do*
anything on its own.
- **They can** `extend` **another container** (inheritance). This is how packets build on headers: a
command packet extends a command header and adds a payload field.
- **Fields can themselves be containers**, allowing nesting (e.g., a telemetry packet has a
`Payload` field whose type is another container).

```335:338:CF.edsl
    // Cancel a transaction
    container CancelCmd extends CMD {
        Transaction_Payload Payload
    }
```

Above, `CancelCmd` inherits the command header (`CMD`) and adds one field, `Payload`, whose type is
the `Transaction_Payload` container defined earlier.

### 4.3 Interfaces

Interfaces are declared in an `interfaceSet { ... }` block and define **abstract contracts** —
named sets of operations and/or parameters that components communicate through. They say *how*
components may talk without saying *which* concrete component is on the other end.

```260:278:CFE_SB.edsl
    application interface Telemetry {
        parameters {
            readOnly async uint16 InstanceNumber
            readOnly async uint16 TopicId
        }
        commands {
            async indication
        }
    }

    application interface Telecommand {
        parameters {
            readOnly async uint16 InstanceNumber
            readOnly async uint16 TopicId
        }
        commands {
            async indication
        }
    }
```

An interface can declare:

- `parameters { ... }` — named values exposed across the interface (e.g., `TopicId`), often
marked `readOnly` and `async`.
- `commands { ... }` — operations that can be invoked over the interface (e.g., `send`,
`receive`, `indication`, `load`, `dump`).

The important interfaces in these files are `Telecommand` and `Telemetry` (defined in `CFE_SB.edsl`)
and `Table` (defined in `CFE_TBL.edsl`). Components then declare that they **provide** or
**require** these interfaces (see below).

### 4.4 Applications (components)

Applications are **components** — the actual software units — declared in a `componentSet { ... }`
block. In these files, each app package defines a component literally named `Application`, which is
the standard cFS app entry point. A component is defined by the interfaces it provides/requires plus
its internal implementation:

```396:420:CF.edsl
    component Application {
        requiredInterfaces {
            Table config_table
            Telecommand CMD
            Telecommand SEND_HK
            Telecommand WAKE_UP
            Telemetry HK_TLM
            Telemetry EOT_TLM
        }
        implementation {
            variables {
                uint16 CmdTopicId
                uint16 SendHkTopicId
                uint16 WakeUpTopicId
                uint16 HkTlmTopicId
                uint16 EotTlmTopicId
            }
            parameterMaps {
                map CMD.TopicId -> CmdTopicId
                map SEND_HK.TopicId -> SendHkTopicId
                map WAKE_UP.TopicId -> WakeUpTopicId
                map HK_TLM.TopicId -> HkTlmTopicId
                map EOT_TLM.TopicId -> EotTlmTopicId
            }
        }
    }
```



#### `requiredInterfaces`

`requiredInterfaces { ... }` lists the interfaces the component **needs from the rest of the
system** — its inputs and dependencies. Each entry is `<InterfaceType> <InstanceName>`. For the CF
app this means: it *requires* a `Table` (`config_table`) to load its configuration, three
`Telecommand` inputs it receives (`CMD`, `SEND_HK`, `WAKE_UP`), and two `Telemetry` outputs it
produces (`HK_TLM`, `EOT_TLM`).

(The counterpart, `providedInterfaces`, lists interfaces a component **offers to others**. The CF
`Application` provides none, but infrastructure components in `CFE_SB.edsl` — like `SoftwareBus`
providing `SoftwareBusAccess` — do. A `provided` interface on one component plugs into a `required`
interface on another.)

#### `implementation`

`implementation { ... }` holds the component's **internal binding details** — how the abstract
interfaces above are realized inside this specific component. It typically contains `variables` and
`parameterMaps`.

- `variables { ... }` declares the component's **own internal storage**, written as
`<type> <name>`. For the CF app these are five `uint16` slots, one to hold the routing/topic ID
for each message stream. Note the difference from container fields: container fields describe a
*data layout*, while these variables are *runtime state owned by the component*. Both use types
defined in a `dataTypeSet`.



#### `parameterMaps`

`parameterMaps { ... }` **wires an interface's parameter to one of the component's variables**, using
`map <Interface>.<Parameter> -> <Variable>`. For example, `map CMD.TopicId -> CmdTopicId` reads as:
"the `TopicId` parameter of the `CMD` interface is stored in this component's `CmdTopicId` variable."
This is what binds the *abstract* `Telecommand`/`Telemetry` interfaces to *concrete* routing IDs at
configuration/build time, so tooling knows exactly which message ID each stream uses.

### 4.5 How the constructs relate


| Construct               | Section        | Represents                                     | Analogy (C)              |
| ----------------------- | -------------- | ---------------------------------------------- | ------------------------ |
| type / `container`      | `dataTypeSet`  | The shape of data (records, messages, packets) | `typedef` / `struct`     |
| interface               | `interfaceSet` | An abstract contract (parameters + commands)   | an abstract API / header |
| component / application | `componentSet` | A software unit that runs and exchanges data   | a module / task instance |


Putting it together: **components (applications)** exchange **data (containers)** by talking over
**interfaces**, and `implementation`/`parameterMaps` bind those abstract interfaces to the
component's concrete internal `variables`.

---



## 5. Data structures reference

EDSL data structures come in a few kinds:

- **IntegerDataType / FloatDataType / BooleanDataType** — scalar numeric/boolean types with an
encoding, bit size, and optional range.
- **StringDataType** — fixed-length text.
- **EnumeratedDataType** — a named set of integer constants.
- **ArrayDataType** — a fixed-length array of another type (length can be a count or an index type).
- **container** — a structured record (like a C `struct`); packets are containers that `extend`
a header container.



### 5.1 `CONFIG.edsl` — configuration constants (no types)

Only constants. The most relevant groups:

`CF` **block**


| Constant                                 | Value         | Meaning                                   |
| ---------------------------------------- | ------------- | ----------------------------------------- |
| `NUM_CHANNELS`                           | 2             | Number of CF output channels              |
| `NAK_MAX_SEGMENTS`                       | 58            | Max NAK segments tracked                  |
| `MAX_POLLING_DIR_PER_CHAN`               | 5             | Max polling directories per channel       |
| `MAX_PDU_SIZE`                           | 512           | Max CFDP PDU size (bytes)                 |
| `FILENAME_MAX_NAME` / `FILENAME_MAX_LEN` | from core API | Filename/path length limits               |
| `PDU_ENCAPSULATION_EXTRA_TRAILING_BYTES` | 0             | Extra trailing bytes on encapsulated PDUs |


`CFE_MISSION` **block (selected):** `DATA_BYTE_ORDER = bigEndian`, `SIGNED_INTEGER_ENCODING = twosComplement`, `MEM_REFERENCE_SIZE_BITS = 64`, `SPACECRAFT_ID = 66`, `CORE_API_MAX_PATH_LEN = 64`,
`CORE_API_MAX_FILE_LEN = 20`, `CORE_API_MAX_API_LEN = 20`, `SB_MAX_PIPES = 64`,
`SB_MAX_SB_MSG_SIZE = 32768`, `MSG_HEADER_TYPE = "SpacePacketBasic"`. These feed the `${...}`
substitutions throughout `BASE_TYPES`, `CFE_SB`, and `CF`.

### 5.2 `CFE_TOPICIDS.edsl` — topic ID constants (no types)

Computes routing ranges and per-app IDs. CF-relevant entries:


| Constant                                  | Meaning                            |
| ----------------------------------------- | ---------------------------------- |
| `CF_CMD_TOPICID`                          | CF command topic                   |
| `CF_SEND_HK_TOPICID`                      | "Send housekeeping" request topic  |
| `CF_WAKE_UP_TOPICID`                      | Periodic wakeup topic              |
| `CF_HK_TLM_TOPICID`                       | CF housekeeping telemetry topic    |
| `CF_EOT_TLM_TOPICID`                      | End-of-Transaction telemetry topic |
| `CF_CH0_RX_TOPICID` / `CF_CH1_RX_TOPICID` | Per-channel receive PDU topics     |
| `CF_CH0_TX_TOPICID` / `CF_CH1_TX_TOPICID` | Per-channel transmit PDU topics    |




### 5.3 `BASE_TYPES.edsl` — primitive types


| Type               | Kind                       | Notes                                                           |
| ------------------ | -------------------------- | --------------------------------------------------------------- |
| `int8`–`int64`     | IntegerDataType (signed)   | Two's-complement, sizes 8/16/32/64 bits                         |
| `uint8`–`uint64`   | IntegerDataType (unsigned) | Sizes 8/16/32/64 bits, with ranges                              |
| `MemReference`     | IntegerDataType            | CPU address/size/offset; width = `MEM_REFERENCE_SIZE_BITS` (64) |
| `float` / `double` | FloatDataType              | IEEE-754 single / double                                        |
| `StatusBit`        | BooleanDataType            | Single true/false bit                                           |
| `ApiName`          | StringDataType             | Length `CORE_API_MAX_API_LEN` (20) — for API/semaphore names    |
| `PathName`         | StringDataType             | Length `CORE_API_MAX_PATH_LEN` (64) — for file paths            |
| `FileName`         | StringDataType             | Length `CORE_API_MAX_FILE_LEN` (20) — for bare filenames        |




### 5.4 `CFE_HDR.edsl` — message headers


| Type              | Kind                        | Fields / notes                                      |
| ----------------- | --------------------------- | --------------------------------------------------- |
| `FunctionCode`    | IntegerDataType (uint8)     | Command code within a command secondary header      |
| `ChecksumType`    | IntegerDataType (uint8)     | Command packet checksum                             |
| `Message`         | container                   | Wraps `SpacePacketBasic CCSDS` (the primary header) |
| `CmdSecHdr`       | container                   | Command secondary header: `FunctionCode`            |
| `TlmSecHdr`       | container                   | Telemetry secondary header: `Seconds`, `Subseconds` |
| `CommandHeader`   | container extends `Message` | Adds `CmdSecHdr Sec` — base for all commands        |
| `TelemetryHeader` | container extends `Message` | Adds `TlmSecHdr Sec` — base for all telemetry       |




### 5.5 `CFE_SB.edsl` — Software Bus

**Types & enums**


| Type                | Kind                     | Notes                                               |
| ------------------- | ------------------------ | --------------------------------------------------- |
| `MsgIdValue`        | IntegerDataType          | Raw message ID value; width = `MSGID_BIT_SIZE` (32) |
| `RouteId`           | IntegerDataType (uint16) | Software Bus route index                            |
| `QosPriority`       | Enum                     | `LOW=0`, `HIGH=1`                                   |
| `QosReliability`    | Enum                     | `LOW=0`, `HIGH=1`                                   |
| `PipeDepthStatsSet` | ArrayDataType            | `PipeDepthStats[SB_MAX_PIPES]`                      |
| `SubEntriesSet`     | ArrayDataType            | `SubEntries[SB_SUB_ENTRIES_PER_PKT]`                |


**Containers (records / packets)**


| Container                                                                                                                                                                                                                | Purpose                                                                |
| ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ---------------------------------------------------------------------- |
| `MsgId`                                                                                                                                                                                                                  | Abstract message identifier wrapping a `MsgIdValue`                    |
| `PipeId`                                                                                                                                                                                                                 | A Software Bus pipe identifier                                         |
| `Qos`                                                                                                                                                                                                                    | Quality of service (`Priority`, `Reliability`)                         |
| `PipeInfoEntry`                                                                                                                                                                                                          | Pipe info file entry (pipe/app names, queue depths, errors)            |
| `WriteFileInfoCmd_Payload`                                                                                                                                                                                               | Filename payload for "write info to file" commands                     |
| `RouteCmd_Payload`                                                                                                                                                                                                       | `MsgId` + `PipeId` for enable/disable route commands                   |
| `HousekeepingTlm_Payload`                                                                                                                                                                                                | SB task HK counters (command/error/pipe/subscribe counters, mem usage) |
| `PipeDepthStats`                                                                                                                                                                                                         | Per-pipe depth statistics                                              |
| `StatsTlm_Payload`                                                                                                                                                                                                       | SB-wide statistics (msg IDs, pipes, memory, subscriptions in use)      |
| `RoutingFileEntry` / `MsgMapFileEntry`                                                                                                                                                                                   | Entries written to routing/map info files                              |
| `SingleSubscriptionTlm_Payload`                                                                                                                                                                                          | One subscription report                                                |
| `SubEntries` / `AllSubscriptionsTlm_Payload`                                                                                                                                                                             | Previous-subscriptions report (segmented)                              |
| `CommandBase`, `SubReportBase`, `SendHkCmd`                                                                                                                                                                              | Command bases extending `CommandHeader`                                |
| `HousekeepingTlm`, `StatsTlm`, `AllSubscriptionsTlm`, `SingleSubscriptionTlm`                                                                                                                                            | Telemetry packets extending `TelemetryHeader`                          |
| `NoopCmd`, `ResetCountersCmd`, `SendSbStatsCmd`, `WriteRoutingInfoCmd`, `EnableRouteCmd`, `DisableRouteCmd`, `WritePipeInfoCmd`, `WriteMapInfoCmd`, `EnableSubReportingCmd`, `DisableSubReportingCmd`, `SendPrevSubsCmd` | The SB command set                                                     |


**Interfaces:** `SoftwareBusRouting` (send/receive), `SoftwareBusAccess` (pub/sub with `MsgId`),
`Telemetry` and `Telecommand` (each exposes `InstanceNumber` + `TopicId` parameters and an
`indication` command). **Components:** `MTS`, `SoftwareBus`, `Listener`, `Publisher`, and the
generic `Application` pattern (required `Telecommand`/`Telemetry` interfaces + `TopicId` variable
maps) that CF and other apps mirror.

### 5.6 `CFE_TBL.edsl` — Table Services

**Types & enums**


| Type                | Kind                         | Notes                                                    |
| ------------------- | ---------------------------- | -------------------------------------------------------- |
| `TableName`         | StringDataType               | Length `TBL_MAX_FULL_NAME_LEN`                           |
| `BufferSelect`      | Enum                         | `INACTIVE=0`, `ACTIVE=1` (which buffer to validate/dump) |
| `HandleId`, `RegId` | container extends `BaseType` | Handle / registry ID types                               |


**Containers**


| Container                                                                                                                                               | Purpose                                                                |
| ------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------- |
| `File_Hdr`                                                                                                                                              | Header for CFE table data files (offset, num bytes, table name)        |
| `CombinedFileHdr`                                                                                                                                       | Standard file header + table header                                    |
| `LoadCmd_Payload`                                                                                                                                       | Filename to load                                                       |
| `DumpCmd_Payload`                                                                                                                                       | Buffer select + table name + dump filename                             |
| `ValidateCmd_Payload`                                                                                                                                   | Buffer select + table name                                             |
| `ActivateCmd_Payload`                                                                                                                                   | Table name                                                             |
| `DumpRegistryCmd_Payload`                                                                                                                               | Dump filename                                                          |
| `SendRegistryCmd_Payload` / `DelCDSCmd_Payload` / `AbortLoadCmd_Payload`                                                                                | Table name payloads                                                    |
| `NotifyCmd_Payload`                                                                                                                                     | Notification parameter                                                 |
| `HousekeepingTlm_Payload`                                                                                                                               | TBL HK (counters, num tables, validation status, last load/dump names) |
| `TblRegPacket_Payload`                                                                                                                                  | Table registry entry (size, CRC, buffer addresses, flags, owner)       |
| `CommandBase`, `SendHkCmd`, `NotifyCmd`                                                                                                                 | Command bases                                                          |
| `HousekeepingTlm`, `TableRegistryTlm`                                                                                                                   | Telemetry packets                                                      |
| `NoopCmd`, `ResetCountersCmd`, `LoadCmd`, `DumpCmd`, `ValidateCmd`, `ActivateCmd`, `DumpRegistryCmd`, `SendRegistryCmd`, `DeleteCDSCmd`, `AbortLoadCmd` | The TBL command set                                                    |


**Interface:** `Table` (`load`, `dump`). **Components:** `TableService` and the app `Application`
pattern.

### 5.7 `CF.edsl` — the CFDP File Transfer application

**Value types & enums**


| Type             | Kind                     | Notes                                                                                                                                                       |
| ---------------- | ------------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `EntityId`       | IntegerDataType (uint32) | CFDP entity ID                                                                                                                                              |
| `TransactionSeq` | IntegerDataType (uint32) | Transaction sequence number                                                                                                                                 |
| `ChannelId`      | IntegerDataType (uint8)  | Channel index, range `0..NUM_CHANNELS`                                                                                                                      |
| `EnableFlag`     | Enum                     | `NO=0`, `YES=1`                                                                                                                                             |
| `CFDP`           | Enum                     | `CLASS_1=0` (unacknowledged), `CLASS_2=1` (acknowledged)                                                                                                    |
| `GetSet_ValueID` | Enum                     | Parameter IDs for Get/Set param commands (`ticks_per_second`, `ack_timer_s`, `nak_timer_s`, `inactivity_timer_s`, `ack_limit`, `nak_limit`, `local_eid`, …) |
| `Reset`          | Enum                     | Reset targets: `all`, `command`, `fault`, `up`, `down`                                                                                                      |
| `QueueIdx`       | Enum                     | Queue indices: `PEND`, `TX`, `RX`, `HIST`, `HIST_FREE`, `FREE`                                                                                              |
| `Type`           | Enum                     | `all`, `up`, `down` (transaction direction filter)                                                                                                          |
| `Queue`          | Enum                     | `pend`, `active`, `history`, `all`                                                                                                                          |


**Array types**


| Type                 | Definition                            |
| -------------------- | ------------------------------------- |
| `PollDirTable`       | `PollDir[MAX_POLLING_DIR_PER_CHAN]`   |
| `ChannelConfigTable` | `ChannelConfig[indexType ChannelId]`  |
| `QSize`              | `uint16[indexType QueueIdx]`          |
| `Channel_Hk`         | `HkChannel_Data[indexType ChannelId]` |
| `Hword`              | `uint16[2]`                           |
| `Byte`               | `uint8[4]`                            |


**Configuration containers**


| Container       | Purpose                                                                                                                           |
| --------------- | --------------------------------------------------------------------------------------------------------------------------------- |
| `PollDir`       | One polling-directory config (interval, priority, CFDP class, dest EID, src/dst dirs, enabled)                                    |
| `ChannelConfig` | Per-channel config (timers, ack/nak limits, input/output MsgIds, pipe depth, poll dirs, semaphore name, dequeue enable, move dir) |
| `ConfigTable`   | Top-level CF config table (ticks/sec, CRC bytes/wakeup, local EID, channel array, chunk size, tmp/fail dirs)                      |


**Housekeeping / telemetry containers**


| Container                         | Purpose                                                                                                                              |
| --------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------ |
| `HKCommandCounters`               | `cmd` and `err` command counters                                                                                                     |
| `HkSent`                          | Sent counters (file data bytes, PDUs, NAK segment requests)                                                                          |
| `HkRecv`                          | Received counters (bytes, PDUs, errors, spurious, dropped, NAK segment requests)                                                     |
| `HkFault`                         | Fault counters (file open/read/seek/write/rename, directory read, CRC mismatch, file-size mismatch, NAK/ACK limit, inactivity timer) |
| `HkCounters`                      | Bundles `HkSent` + `HkRecv` + `HkFault`                                                                                              |
| `HkChannel_Data`                  | Per-channel HK (counters, queue sizes, poll/playback counters, frozen flag)                                                          |
| `HkPacket_Payload` / `HkPacket`   | Full housekeeping telemetry packet (command counters + per-channel data)                                                             |
| `TxnFilenames`                    | Cached source + destination filenames for a transaction                                                                              |
| `EotPacket_Payload` / `EotPacket` | End-of-Transaction telemetry (seq num, channel, direction, state, status, src/peer EID, file size, CRC result, filenames)            |


**Command payloads**


| Container             | Purpose                                                                                   |
| --------------------- | ----------------------------------------------------------------------------------------- |
| `UnionArgs_Payload`   | Generic 4-byte argument wrapper (`Byte`)                                                  |
| `GetParam_Payload`    | Get-parameter (`key`, `chan_num`)                                                         |
| `SetParam_Payload`    | Set-parameter (`value_`, `key`, `chan_num`)                                               |
| `TxFile_Payload`      | Transfer-file args (CFDP class, keep flag, channel, priority, dest ID, src/dst filenames) |
| `WriteQueue_Payload`  | Write-queue args (type, channel, queue, filename)                                         |
| `Transaction_Payload` | Transaction identifier (`ts`, `eid`, `chan`)                                              |


**Commands (all extend** `CMD`**, which extends** `CommandHeader`**)**


| Command                                        | Requirement it realizes (examples)           |
| ---------------------------------------------- | -------------------------------------------- |
| `NoopCmd`                                      | CF1000 No-Op                                 |
| `ResetCountersCmd`                             | CF1001 Reset counters                        |
| `TxFileCmd`                                    | CF3000 Transfer File                         |
| `PlaybackDirCmd`                               | CF3001 Playback Directory                    |
| `FreezeCmd` / `ThawCmd`                        | CF5000 / CF5001 Freeze / Thaw channel        |
| `SuspendCmd` / `ResumeCmd`                     | CF5016–CF5019 Suspend / Resume               |
| `CancelCmd` / `AbandonCmd`                     | CF5005 / CF5006 Cancel / Abandon transaction |
| `SetParamCmd` / `GetParamCmd`                  | CF5002 / CF5003 Set / Get protocol parameter |
| `WriteQueueCmd`                                | CF5021 Write queue to file                   |
| `EnableDequeueCmd` / `DisableDequeueCmd`       | Channel dequeue (tx) enable/disable          |
| `EnableDirPollingCmd` / `DisableDirPollingCmd` | CF4001 Enable / Disable polling              |
| `PurgeQueueCmd`                                | CF5020 Purge queue                           |
| `EnableEngineCmd` / `DisableEngineCmd`         | CF8000 / CF8001 Engine enable / disable      |
| `SendHkCmd`, `WakeupCmd`                       | Periodic housekeeping request / wakeup       |


**Component & metadata:** the CF `Application` component requires a `config_table` (Table),
`CMD`/`SEND_HK`/`WAKE_UP` telecommands, and `HK_TLM`/`EOT_TLM` telemetry, mapping each to a
`TopicId` variable. The `metadata` block records cFS bindings (`appName = "CF"`, command/HK
MsgIDs, pipe depth).

---



## 6. How this ties back to `main.py`

`main.py` uses sentence-transformer embeddings and cosine similarity to match a list of
**variables** against a list of **sentences**. In this domain that maps naturally to building a
**requirements-to-design traceability matrix**: the requirement descriptions in
`cf_FunctionalRequirements.csv` are the *sentences*, and the containers/fields/enums declared in
the EDSL files (e.g., `EotPacket`, `frozen`, `ack_limit`, `dequeue_enabled`) are the *variables*.
Cosine similarity then links each model element to the requirement(s) it most likely implements.