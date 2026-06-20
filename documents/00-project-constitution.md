# Project Constitution

***name*** Document Management

## Overview

This project is to create a Document Management System. This product is a Document Management system that will allow complex specifications and collaboration materials to be stored in a text format that machines and robots can read, while also providing enough markup to produce rich documents that people can understand and render into PDF for delivery to 3rd parties that cannot access the repositories.  

Documents will be composed of parts (other documents included in an aggregate) that allow for sections, chapters, tables, and any partial section to be written in an independent document and included in the "main" document. 

Documents and partial documents will be represented as a complex graph, where a document can be referenced and pulled into any other document. While the base level structure will be a tree of folders and documents, documents should be free to reference any document in the entire structure.

Documents should be taggable for searching, and a full representation of a document that combines all partials should be able to be built at any time and stored as a numbered version. The system will keep track of numbered versions. Each numbered version will be available as both a text and a PDF. 

### Vision
1. A comprehensive document management system that allows large collections of working materials to be combined into complete documents that can be versioned and provided to 3rd parties.
2. A document management system that provides all text to be stored in a form that can be easily consumed and indexed by LLMs for reference and integration.
3. A system that is easy to navigate by document authors and researchers that allows for many authors to work at the same time within the document repository/library.
4. An artifact management system that allows for other materials, such as charts, graphs, and tables (CSV/Excel), to be pulled into documentation easily for inclusion as reference materials. Often, the same chart, graph, table, or graphic is needed in multiple documents. The ability to provide both the artifact and the description of the artifact in plain text.
5. A meta system is provided that allows for comments and conversation to occur over a document. This meta system is also stored in text form within adjacent data structures in the repository.

### Objective 
1. Reduce the amount of time required to maintain specifications and complex working materials for projects by providing a repository that can reference all materials.
2. Create a rich ecosystem where both document materials and artifacts all reside along side each other and can be easily encorperated into each other.
3. A system that is both people-readable and maintainable but also allows for robots to work on documents, partials, and other artifacts in a way that multiple people can work together. 

### Guiding Principles
* Text is the canonical representation: The text version is the authoritative, definitive source of truth; artifacts that are deployed, such as PDFs, will be produced from the text. The text remains the source for all further offspring. 
* Every generated document is reproducible: Every Document when versioned and produced must have a text version of it as well that is also versioned and number so that it can be reproduced from that text version.
* References must be resolvable and traceable.
* No change may silently overwrite another author's work.
* Published versions are immutable.
* Human-readable and machine-readable representations must remain aligned.
* Artifacts require metadata and accessible textual descriptions.
* [Example need - Customer Insight Documentation System Vision](./00.01-Interview-Constitution.md)

### Scope
* Document Management
  * Authoring source documents
  * Modifying source documents
  * identifying main documents
  * tagging documents
  * References and dependency management
  * Artifact registration and reuse
  * Search and classification
  * Reusable document composition
  * Preivew of Documennt including inclusion of Partials
  * Review of PDF render would look like
  * WYSIWYG editor for creating the text and layout
    * Tables
    * Partials includes
    * Artifact embedding
* Artifact Management
  * Artifact registration and reuse
  * Uploading artifacts
  * References and dependency management
  * Artifact registration and reuse
  * Search and classification
  * Reusable document composition
* Diagram Management
  * Uploading new diagrams
  * Example: https://github.com/jgraph/mxgraph
* Publication
  *  Versioned publication
  * PDF rendering
* Meta Management 
  * Document Graph is searchable and taggable 
  * Documents and artifacts can be commented on referencing lines or objects within the artifact
  * All artifacts and documents can be referenced and included into other objects
  * partial documents such as a paragraph or series of lines can be referenced in another document

### Dictionary
***artifact***: is any object that can be stored within a computer system as a single binary object. Items such as plain text files, images, pdf, markdown files, CSVs, excel documents, etc.

***Document***:  is any plain text artifact that's intended purposed to to describe.

***aggragated document*** is a document composed out of text and tokens that idicate where and what other documents should be imported. The document itself is composed of its own descriptions and also inclusion points for other documents or other artifacts such as graphs, images, charts, or tables. 

***Numbered version*** is a snapshot (duplication of a document) of a aggragated document at a specific time captured and numbered so that it can be references as a historical artifact.

***main document*** is a document designated as an entry point into a graph of documents and artifacts. It can be pointed to as a place to start reading from to produce a output artifact such as PDF.

***repository*** is a storage system that contains documents, artifacts, meta data, and any other resource that could be used in the document and management.

***library*** is a series of main documents that are associated together to produce a meaningful collection of ideas.